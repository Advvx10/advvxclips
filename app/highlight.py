"""Detección de momentos destacados (100 % local, sin IA ni API).

Trabaja sobre palabras con timestamps exactos [{w,s,e,p}] y, opcionalmente, la energía de audio por segundo.
Genera ventanas que empiezan y terminan en límites de frase, las puntúa y devuelve las mejores sin solapes.
La IA (ai.py) solo reordena y titula estos candidatos; si no hay IA, este puntaje es el definitivo.
"""
import re
import statistics

_END = re.compile(r"[.!?…]+[\"')»”]*$")
_WORD = re.compile(r"[\wáéíóúüñÁÉÍÓÚÜÑ']+", re.U)

HOOK_WORDS = {
    "mira", "escucha", "nunca", "jamás", "jamas", "secreto", "secretos", "increíble", "increible", "error", "errores",
    "verdad", "mentira", "nadie", "cuidado", "ojo", "imagina", "locura", "brutal", "urgente", "peligro", "gratis",
    "truco", "trucos", "descubrí", "descubri", "sorpresa", "impactante", "atención", "atencion", "ganó", "gané",
    "perdí", "perdi", "millones", "millón", "récord", "record", "demanda", "escándalo", "escandalo", "viral",
    "never", "secret", "truth", "lie", "nobody", "mistake", "crazy", "imagine", "stop", "warning", "free", "trick",
    "shocking", "listen", "wait", "insane", "million", "record", "scandal", "banned", "exposed",
    "nunca", "jamais", "segredo", "verdade", "loucura", "ninguém", "ninguem",
}
HOOK_PHRASES = ("por qué", "por que", "cómo", "como hacer", "lo que nadie", "lo que no te", "la razón", "la razon",
                "te voy a", "vas a ", "no vas a creer", "you won't", "here's why", "the reason", "what nobody",
                "how to", "the truth", "o que ninguém", "você não")
EMOTION = {
    "amor", "odio", "miedo", "feliz", "triste", "llorar", "lloré", "risa", "gracioso", "loco", "loca", "wow", "dios",
    "madre", "mierda", "carajo", "épico", "epico", "genial", "terrible", "horrible", "peor", "mejor", "milagro",
    "traición", "traicion", "muerte", "puta", "joder", "coño", "verga", "qué", "jaja", "jajaja",
    "love", "hate", "fear", "happy", "sad", "cry", "funny", "insane", "amazing", "worst", "best", "epic", "omg",
    "shit", "damn", "fuck", "lol", "lmao", "bro", "dude", "holy",
}
FILLER = {"eh", "ehh", "mm", "mmm", "este", "pues", "osea", "bueno", "um", "uh", "uhm", "like", "ajá", "aja", "sabes",
          "entonces", "digamos", "tipo"}
CONTINUERS = {"y", "pero", "entonces", "porque", "pues", "así", "asi", "and", "but", "so", "because", "then", "e", "mas"}

# --- detector de momentos (v3.1) ---
def _is_laugh_word(w: str) -> bool:
    """True si la palabra es una risa (jaja/jeje/jiji/[risa]). Por palabra
    completa: «prisa» NO es risa."""
    lw = (w or "").lower().strip(".,!¡?\"'»”[]()")
    return "jaja" in lw or "jeje" in lw or "jiji" in lw or lw == "risa"


def _has_laugh(text: str) -> bool:
    return any(_is_laugh_word(w) for w in words_in(text or ""))
INSULT_WORDS = {"cállate", "callate", "mentiroso", "mentirosa", "vergüenza", "verguenza", "imbécil", "imbecil",
                "estúpido", "estupido", "estúpida", "estupida", "idiota", "mentira", "mentiras"}
INSULT_PHRASES = ("tú no", "tu no", "no me", "deja de", "no jodas")
MOMENT_BONUS = {"Pelea": 18, "Discusión": 14, "Risa": 14, "Gritos": 12, "Momento gracioso": 8}
MOMENT_TITLES = {
    "Pelea": "Se pelean en pleno directo",
    "Discusión": "Discusión fuerte en el directo",
    "Risa": "No aguantó la risa en el directo",
    "Gritos": "Gritos en pleno directo",
    "Momento gracioso": "El momento más gracioso del directo",
}


def words_in(text: str) -> list[str]:
    return _WORD.findall(text or "")


def sentences(words: list[dict]) -> list[dict]:
    """Frases (por puntuación o pausa > 0.8 s) con índices de palabra."""
    out, i0 = [], 0
    for i, w in enumerate(words):
        last = i == len(words) - 1
        gap = (words[i + 1]["s"] - w["e"]) if not last else 99
        if _END.search(w["w"].strip()) or gap > 0.8 or last:
            seg = words[i0:i + 1]
            out.append({"s": seg[0]["s"], "e": seg[-1]["e"], "i0": i0, "i1": i,
                        "text": " ".join(x["w"].strip() for x in seg).strip()})
            i0 = i + 1
    return out


def _prefix(vals):
    p = [0.0]
    for v in vals:
        p.append(p[-1] + v)
    return p


def _hook(head: str) -> float:
    low = head.lower()
    ws = [w.lower() for w in words_in(head)]
    s = 0.0
    if "?" in head or "¿" in head:
        s += 0.45
    if "!" in head or "¡" in head:
        s += 0.2
    if any(w in HOOK_WORDS for w in ws[:25]):
        s += 0.4
    if any(p in low for p in HOOK_PHRASES):
        s += 0.3
    if ws and ws[0].isdigit():
        s += 0.2
    return min(1.0, s)


def _energy_stats(energy):
    vals = [max(-60.0, e) for e in energy] if energy else []
    if len(vals) < 20:
        return None
    return statistics.fmean(vals), (statistics.pstdev(vals) or 1.0), vals


def _moment_label(laughs: int, insults: int, excl: int, sexcl: int, max_run: int, sustained: int,
                  wpm_ratio: float, hook: float, numbers: float, text_low: str) -> str:
    """Tipo de momento dominante de una ventana, en español.

    Aproximación honesta: Whisper no dice quién habla, así que «voces que se
    pisan» se infiere de frases cortas exclamadas seguidas + ritmo + insultos.
    """
    if (insults >= 2 and excl >= 2) or (insults >= 1 and max_run >= 3) or (insults >= 2 and sustained >= 3):
        return "Pelea"
    if max_run >= 3 or (excl >= 3 and wpm_ratio >= 1.15) or sexcl >= 4:
        return "Discusión"
    if laughs >= 2 or (laughs >= 1 and sustained >= 2):
        return "Risa"
    if sustained >= 3 and excl >= 1:
        return "Gritos"
    if laughs >= 1 or "gracioso" in text_low or "chiste" in text_low:
        return "Momento gracioso"
    if numbers >= 0.6 and hook < 0.45:
        return "Dato curioso"
    return "Idea clave"


def _diversify(cands: list[dict], top_n: int = 8) -> list[dict]:
    """Reordena para que entre los top_n haya al menos un candidato de cada
    tipo de momento presente (round-robin por tipo, empezando por el mejor
    candidato global). No cambia el conjunto, solo el orden."""
    if len(cands) <= 1:
        return cands
    groups: dict[str, list[dict]] = {}
    for c in cands:
        groups.setdefault(c.get("moment") or "Idea clave", []).append(c)
    order = sorted(groups, key=lambda m: -groups[m][0]["score"])
    idx = {m: 0 for m in order}
    top: list[dict] = []
    while len(top) < min(top_n, len(cands)):
        progressed = False
        for m in order:
            if len(top) >= min(top_n, len(cands)):
                break
            if idx[m] < len(groups[m]):
                top.append(groups[m][idx[m]])
                idx[m] += 1
                progressed = True
        if not progressed:
            break
    used = {id(c) for c in top}
    return top + [c for c in cands if id(c) not in used]


def candidates(words: list[dict], energy: list[float] | None, lo: float, hi: float, k: int = 24,
               content_type: str = "general") -> list[dict]:
    """Devuelve hasta k ventanas sin solape, mejores primero:
    [{start,end,score,feat,text,i0,i1,reason}]  (start/end exactos de palabra).
    content_type (v3.1.4): entrevista/podcast premian discusión, cruce de
    voces e intercambio rápido; resumen premia ventanas narrativas largas;
    gaming premia energía alta; pelicula/general no cambian nada."""
    words = [w for w in words if w["w"].strip()]
    sents = sentences(words)
    if not sents:
        return []
    lows = [[x.lower() for x in words_in(s["text"])] for s in sents]
    n_w = _prefix([len(l) for l in lows])
    n_emo = _prefix([sum(1 for x in l if x in EMOTION) + 0.5 * sum(1 for w in words[s["i0"]:s["i1"] + 1]
                                                                 if len(w["w"]) > 2 and w["w"].isupper())
                     + s["text"].count("!") + s["text"].count("¡") for l, s in zip(lows, sents)])
    n_fill = _prefix([sum(1 for x in l if x in FILLER) for l in lows])
    n_dig = _prefix([sum(1 for w in words[s["i0"]:s["i1"] + 1] if any(c.isdigit() for c in w["w"])) for s in sents])
    n_q = _prefix([1 if "?" in s["text"] else 0 for s in sents])
    # señales por frase para el detector de momentos (v3.1)
    laugh_c = [sum(1 for w in words[s["i0"]:s["i1"] + 1] if _is_laugh_word(w["w"])) for s in sents]
    ins_c = [sum(1 for x in l if x in INSULT_WORDS)
             + sum(1 for p in INSULT_PHRASES if p in s["text"].lower())
             for l, s in zip(lows, sents)]
    excl = [1 if ("!" in s["text"] or "¡" in s["text"]) else 0 for s in sents]
    short_excl = [1 if excl[i] and len(lows[i]) < 8 else 0 for i in range(len(sents))]
    n_laugh = _prefix(laugh_c)
    n_ins = _prefix(ins_c)
    n_excl = _prefix(excl)
    n_sexcl = _prefix(short_excl)
    total_w = sum(len(l) for l in lows)
    video_dur = max(words[-1]["e"] - words[0]["s"], float(len(energy)) if energy else 0.0, 1.0)
    avg_wpm = total_w / video_dur * 60.0
    es = _energy_stats(energy)
    if es:
        gmean, gstd, ev = es
        ep = _prefix(ev)
    # frases «calientes»: exclamación, risa, insulto, energía alta o humor.
    # Sirven para medir qué parte de la ventana ES el momento (cobertura).
    if es:
        gmean_e, gstd_e, ev_e = es
        sent_hot_e = []
        for s in sents:
            j0, j1 = max(0, int(s["s"])), min(len(ev_e), int(s["e"]) + 1)
            m = statistics.fmean(ev_e[j0:j1]) if j1 > j0 else gmean_e
            sent_hot_e.append(1 if m > gmean_e + gstd_e else 0)
    else:
        sent_hot_e = [0] * len(sents)
    hot_w = [len(lows[i]) if (excl[i] or laugh_c[i] or ins_c[i] or sent_hot_e[i]
                              or "gracioso" in lows[i] or "chiste" in lows[i]) else 0
             for i in range(len(sents))]
    n_hot = _prefix(hot_w)

    target = (lo + hi) / 2
    cands = []
    for a, sa in enumerate(sents):
        best = None
        for b in range(a, len(sents)):
            dur = sents[b]["e"] - sa["s"]
            if dur > hi:
                break
            if dur < lo * 0.85:
                continue
            nw = max(1.0, n_w[b + 1] - n_w[a])
            head = " ".join(s["text"] for s in sents[a:b + 1] if s["s"] < sa["s"] + 8) or sa["text"]
            hook = _hook(head)
            density = max(0.0, min(1.0, (nw / dur - 1.3) / 2.0))
            emotion = min(1.0, (n_emo[b + 1] - n_emo[a]) / nw * 100 / 6.0)
            numbers = min(1.0, (n_dig[b + 1] - n_dig[a]) / 3.0)
            filler = (n_fill[b + 1] - n_fill[a]) / nw
            ends_clean = 1.0 if _END.search(sents[b]["text"].strip()) else 0.0
            ques = 1.0 if (n_q[b + 1] - n_q[a]) > 0 else 0.0
            energy_s = 0.0
            e_max_z, quiet_frac, peak_off, sustained = 0.0, 0.0, None, 0
            if es:
                i0, i1 = max(0, int(sa["s"])), min(len(ev), int(sents[b]["e"]) + 1)
                if i1 > i0:
                    seg_all = ev[i0:i1]
                    seg = sorted(seg_all, reverse=True)[:max(3, (i1 - i0) // 10)]
                    z = (statistics.fmean(seg) - gmean) / gstd
                    energy_s = max(0.0, min(1.0, z / 2.2))
                    e_max_z = (max(seg_all) - gmean) / gstd
                    quiet_frac = sum(1 for v in seg_all if v < gmean - 0.5 * gstd) / len(seg_all)
                    sustained = sum(1 for v in seg_all if v > gmean + gstd)
                    if e_max_z > 0.5:
                        peak_off = (i0 + seg_all.index(max(seg_all))) - sa["s"]
            cont = 1.0 if (lows[a] and lows[a][0] in CONTINUERS) else 0.0
            # --- detector de momentos (v3.1): solo palabras + energía ---
            n_laugh_w = n_laugh[b + 1] - n_laugh[a]
            n_ins_w = n_ins[b + 1] - n_ins[a]
            n_excl_w = n_excl[b + 1] - n_excl[a]
            n_sexcl_w = n_sexcl[b + 1] - n_sexcl[a]
            max_run = 0
            if n_sexcl_w >= 3:
                run = 0
                for j in range(a, b + 1):
                    run = run + 1 if short_excl[j] else 0
                    max_run = max(max_run, run)
            wpm_ratio = (nw / dur * 60.0) / avg_wpm if avg_wpm else 1.0
            if peak_off is None:
                for j in range(a, b + 1):
                    if excl[j] or laugh_c[j]:
                        peak_off = sents[j]["s"] - sa["s"]
                        break
            win_text_low = " ".join(s["text"] for s in sents[a:b + 1]).lower()
            moment = _moment_label(n_laugh_w, n_ins_w, n_excl_w, n_sexcl_w, max_run, sustained,
                                   wpm_ratio, hook, numbers, win_text_low)
            # el bonus del momento se cobra según la cobertura: una ventana
            # que es 90 % charla tranquila y 10 % pelea no merece +18 entero;
            # así la pelea limpia le gana a la ventana larga que se la traga.
            coverage = (n_hot[b + 1] - n_hot[a]) / nw if nw else 0.0
            moment_pts = MOMENT_BONUS.get(moment, 0) * coverage
            # gancho primero: el pico (risa/grito/primera exclamación) al inicio del clip
            hook_first = 4.0 if peak_off is not None and peak_off <= 3 else (
                2.0 if peak_off is not None and peak_off <= 6 else 0.0)
            # penalización «tragado»: tramo tranquilo largo + pico fuerte de energía
            swallow = 8.0 * quiet_frac if (es and quiet_frac >= 0.25 and e_max_z > 1.5) else 0.0
            # --- «De qué trata el video» (v3.1.4): el tipo de contenido
            # inclina la selección, como en el SamuelClip del amigo ---
            ct = str(content_type or "general").lower()
            ct_bonus = 0.0
            if ct in ("entrevista", "podcast"):
                # discusión / cruce de voces / intercambio rápido
                if moment in ("Discusión", "Pelea"):
                    ct_bonus = 8.0
                elif wpm_ratio >= 1.15 and n_sexcl_w >= 1:
                    ct_bonus = 5.0
                elif wpm_ratio >= 1.15:
                    ct_bonus = 3.0
            elif ct == "resumen":
                # ventanas narrativas largas: más duración, idea completa
                ct_bonus = 6.0 * max(0.0, min(1.0, (dur - lo) / max(1.0, hi - lo)))
                if ends_clean:
                    ct_bonus += 2.0
            elif ct == "gaming":
                # momentos de energía alta
                ct_bonus = 8.0 * energy_s
            # pelicula y general: sin cambio
            score = (34 + 20 * hook + 12 * density + 11 * emotion + 5 * numbers + 6 * ends_clean + 4 * ques
                     + 11 * energy_s - 40 * min(0.4, filler) - 6 * cont
                     - min(6.0, abs(dur - target) / target * 8)
                     + moment_pts + hook_first - swallow + ct_bonus)
            if best is None or score > best[0]:
                best = (score, b, dict(hook=hook, density=density, emotion=emotion, numbers=numbers, filler=filler,
                                       ends_clean=ends_clean, energy=energy_s, question=ques, moment=moment), moment)
        if best:
            sc, b, f, moment = best
            cands.append({"score": sc, "a": a, "b": b, "feat": f, "moment": moment})
    cands.sort(key=lambda c: -c["score"])
    out = []
    for c in cands:
        sa, sb = sents[c["a"]], sents[c["b"]]
        if all(sb["e"] <= o["start"] or sa["s"] >= o["end"] for o in out):
            text = " ".join(s["text"] for s in sents[c["a"]:c["b"] + 1])
            out.append({"start": sa["s"], "end": sb["e"], "score": int(round(max(30, min(94, c["score"])))),
                        "feat": c["feat"], "text": text, "i0": sa["i0"], "i1": sb["i1"],
                        "reason": _reason(c["feat"], c["moment"]), "moment": c["moment"],
                        "summary": _summary(text)})
        if len(out) >= k:
            break
    return _diversify(out)


def _reason(f: dict, moment: str | None = None) -> str:
    bits = []
    moment_bits = {
        "Pelea": "Pelea: gritos e insultos en pleno directo",
        "Discusión": "Discusión: intercambio rápido con exclamaciones",
        "Risa": "Risa: risas en pleno directo",
        "Gritos": "Gritos: energía alta sostenida en el audio",
        "Momento gracioso": "Momento gracioso en el directo",
        "Dato curioso": "Dato curioso con datos concretos",
    }
    if moment in moment_bits:
        bits.append(moment_bits[moment])
    if f["hook"] >= 0.45:
        bits.append("gancho fuerte al inicio")
    if f["energy"] >= 0.5:
        bits.append("pico de energía/emoción en el audio")
    if f["emotion"] >= 0.35:
        bits.append("alta carga emocional")
    if f["density"] >= 0.55:
        bits.append("ritmo ágil")
    if f["numbers"] >= 0.6:
        bits.append("datos concretos")
    if f["question"]:
        bits.append("plantea una pregunta")
    if f["ends_clean"]:
        bits.append("cierre claro")
    if not bits:
        return "Momento con buen ritmo de habla"
    return bits[0][:1].upper() + bits[0][1:] + (", " + ", ".join(bits[1:]) if len(bits) > 1 else "")


_OPEN = re.compile(r"^(?:bueno|pues|y|o sea|entonces|este|eh+|mm+|well|so|and|um+|uh+|pero|but)[,\s]+", re.I)


_GREET = re.compile(r"^(hola|buenas|qué tal|que tal|bienvenidos|bienvenidas|saludos)[,\s]+", re.I)


def _split_sents(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"(?<=[.!?…])\s+", (text or "").strip()) if x.strip()]


def _content_sentence(text: str) -> str:
    """Primera frase con contenido del fragmento (sin saludos ni relleno al inicio).
    Prefiere frases sin risas; si no hay ninguna, la primera frase con contenido."""
    sents = _split_sents(text)
    for min_content in (4, 3):
        for s in sents:
            if _has_laugh(s):
                continue
            t = _GREET.sub("", _OPEN.sub("", s).strip()).strip()
            content = [w for w in words_in(t) if w.lower() not in FILLER and len(w) > 2]
            if len(content) >= min_content:
                return t
    for s in sents:
        t = _GREET.sub("", _OPEN.sub("", s).strip()).strip()
        content = [w for w in words_in(t) if w.lower() not in FILLER and len(w) > 2]
        if len(content) >= 4:
            return t
    return sents[0] if sents else ""


def _summary(text: str, max_len: int = 110) -> str:
    """Resumen de una línea para la tarjeta del clip: primera frase con contenido."""
    s = _content_sentence(text)
    if len(s) > max_len:
        s = (s[:max_len - 1].rsplit(" ", 1)[0].rstrip(" ,;:.-") or s[:max_len - 1]) + "…"
    return (s[:1].upper() + s[1:]) if s else ""


def make_summary_title(text: str, moment: str | None = None) -> str:
    """Título resumen (sin IA): la frase con más contenido del fragmento,
    con palabras que realmente se dicen. NUNCA usa una etiqueta de momento
    como título: el usuario rechazó expresamente "Se pelean en pleno
    directo" y "Discusión fuerte en el directo" (v3.1.2) — el título debe
    decir de qué trata el clip, no qué tipo de momento es. Si no hay
    ninguna frase con contenido (solo risas o relleno), el título es el
    neutro "Clip destacado", no una plantilla de momento. Nunca usa una
    frase suelta con risas («jajaja») como título."""
    best, bv = "", -1e9
    for s in _split_sents(text):
        if _has_laugh(s):
            continue
        t = _OPEN.sub("", s).strip(" -–—,;:.!?")
        ws = words_in(t)
        if len(ws) < 3:
            continue
        # sin palabras con contenido (solo relleno: eh, pues, este, bueno…)
        # la frase no sirve de título: que caiga a la plantilla, no a esto
        content = [w for w in ws if w.lower() not in FILLER and len(w) > 2]
        if len(content) < 3:
            continue
        v = 0.0
        v += 2.0 * sum(1 for w in ws if w.lower() in HOOK_WORDS)
        v += 1.0 * sum(1 for w in ws if any(c.isdigit() for c in w))
        v += 1.5 if 4 <= len(ws) <= 9 else (0.5 if len(ws) <= 12 else -1.0)
        if "?" in s or "¿" in s:
            v += 1.0
        if v > bv:
            best, bv = t, v
    if best:
        ws = best.split()
        if len(ws) > 9:
            best = " ".join(ws[:9])
        if len(best) > 60:
            best = (best[:59].rsplit(" ", 1)[0].rstrip(" ,;:.-") or best[:59]) + "…"
        return best[:1].upper() + best[1:]
    # Sin frase con contenido: título neutro. Las plantillas de momento
    # (MOMENT_TITLES: "Se pelean en pleno directo", etc.) NUNCA se usan
    # como título (v3.1.2: el usuario las rechazó); el `moment` del clip
    # sigue guardándose aparte como etiqueta del tipo de momento.
    return "Clip destacado"


def make_title(text: str, max_len: int = 60) -> str:
    """Título gancho: la mejor frase del arranque, limpia y recortada en palabra completa."""
    sents = [x.strip() for x in re.split(r"(?<=[.!?…])\s+", (text or "").strip()) if x.strip()]
    if not sents:
        return "Clip destacado"
    best, bv = sents[0], -9.0
    for i, s in enumerate(sents[:5]):
        v = 0.0
        if "?" in s or "¿" in s:
            v += 2
        if _hook(s) > 0.3:
            v += 2
        if any(c.isdigit() for c in s):
            v += 1
        if 25 <= len(s) <= 80:
            v += 1.5
        elif len(s) < 12:
            v -= 3
        v += max(0.0, 1.2 - 0.3 * i)
        if v > bv:
            best, bv = s, v
    t = _OPEN.sub("", best).strip(" -–—,;:")
    if len(t) < 8:
        t = best.strip()
    if len(t) > max_len:
        t = (t[:max_len - 1].rsplit(" ", 1)[0].rstrip(" ,;:.-") or t[:max_len - 1]) + "…"
    return t[:1].upper() + t[1:]
