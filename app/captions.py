"""Subtítulos estilo viral (ASS) con resaltado palabra por palabra.

Entrada: palabras con tiempos relativos al clip [{w, s, e}]. Cada palabra hablada se resalta justo cuando se
dice (timestamps de Whisper), con un pequeño "pop". Los temas definen tipografía, colores y posición.
"""
import re

_END = re.compile(r"[.!?…]+[\"')»”]*$")
_SOFT = re.compile(r"[,;:–—]+$")

THEMES = {
    "impact": dict(label="Impact · amarillo", font="Poppins", upper=True, size=0.052, primary="FFFFFF", active="FFE600",
                   outline="000000", outline_k=0.13, shadow=2, pop=118, mode="highlight"),
    "neon": dict(label="Neón · cian", font="Poppins", upper=True, size=0.050, primary="FFFFFF", active="00F0FF",
                 outline="7A00FF", outline_k=0.14, shadow=0, pop=120, mode="highlight", blur=1.2),
    "fuego": dict(label="Fuego · naranja", font="Poppins", upper=True, size=0.052, primary="FFFFFF", active="FF7A00",
                  outline="2B0000", outline_k=0.14, shadow=2, pop=122, mode="highlight"),
    "karaoke": dict(label="Karaoke · progresivo", font="Poppins", upper=False, size=0.048, primary="FFFFFF",
                    active="39FF14", dim="9AA0A6", outline="000000", outline_k=0.12, shadow=2, pop=108, mode="progressive"),
    "caja": dict(label="Caja negra", font="Poppins", upper=False, size=0.044, primary="FFFFFF", active="FFD400",
                 outline="000000", outline_k=0.0, shadow=0, pop=100, mode="highlight", box=True),
    "limpio": dict(label="Limpio · minimalista", font="Poppins", upper=False, size=0.042, primary="FFFFFF",
                   active="FFFFFF", outline="000000", outline_k=0.09, shadow=3, pop=100, mode="highlight"),
}


def ass_color(rgb: str, alpha: int = 0) -> str:
    rgb = rgb.lstrip("#")
    r, g, b = rgb[0:2], rgb[2:4], rgb[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper().replace("&H", "&H", 1)


def _t(sec: float) -> str:
    cs = int(round(max(0.0, sec) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"


def _clean(w: str) -> str:
    return (w or "").replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ").strip()


def build_chunks(words: list[dict], vertical: bool = True) -> list[list[dict]]:
    """Agrupa palabras en bloques cortos y legibles (pausas, puntuación, ancho)."""
    max_words, max_chars = (3, 17) if vertical else (6, 38)
    chunks, cur = [], []
    for w in words:
        if not _clean(w.get("w", "")):
            continue
        if cur:
            gap = w["s"] - cur[-1]["e"]
            txt = " ".join(_clean(x["w"]) for x in cur + [w])
            last = _clean(cur[-1]["w"])
            if (len(cur) >= max_words or len(txt) > max_chars or gap > 0.5 or _END.search(last)
                    or (_SOFT.search(last) and len(cur) >= 2)):
                chunks.append(cur)
                cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def _line_limit(width: int, size: int, upper: bool) -> int:
    """Caracteres que caben en una línea (ancho útil / ancho medio del glifo de Poppins Bold)."""
    return max(8, int(width * 0.86 / (size * (0.70 if upper else 0.585))))


def _line_break(texts: list[str], limit: int) -> int:
    """Índice tras el que partir en dos líneas (o -1 si cabe en una)."""
    if len(texts) < 2 or len(" ".join(texts)) <= limit:
        return -1
    best, bd = -1, 10 ** 9
    for i in range(len(texts) - 1):
        d = abs(len(" ".join(texts[:i + 1])) - len(" ".join(texts[i + 1:])))
        if d < bd:
            best, bd = i, d
    return best


ANIMATIONS = {"pop": "Pop (por defecto)", "none": "Sin animación", "fade": "Aparecer suave", "slide": "Deslizar arriba",
              "bounce": "Rebote", "typewriter": "Máquina de escribir", "zoom": "Zoom de entrada"}


def build_ass(words: list[dict], width: int, height: int, theme: str = "impact", position: str = "low",
              anim: str | None = None, size_mult: float = 1.0, extra_styles: str = "",
              extra_events: list | None = None) -> str:
    """anim=None conserva exactamente el comportamiento original del tema (pop por palabra)."""
    th = THEMES.get(theme) or THEMES["impact"]
    vertical = height >= width
    size = round(th["size"] * (height if vertical else height * 1.35) * max(0.5, min(2.0, size_mult)))
    outline = round(size * th["outline_k"], 1)
    align, margin_v = {"low": (2, round(height * (0.26 if vertical else 0.09))),
                       "center": (5, 0), "top": (8, round(height * (0.16 if vertical else 0.08)))}.get(position, (2, 0))
    box = th.get("box")
    back = ass_color("000000", 0x40) if box else ass_color("000000", 0x80)
    out_c = ass_color(th["outline"])
    border_style = 3 if box else 1
    if box:
        outline = round(size * 0.28, 1)
        out_c = ass_color("101014", 0)
    head = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{th['font']},{size},{ass_color(th['primary'])},{ass_color(th['primary'])},{out_c},{back},"
        f"-1,0,0,0,100,100,0.5,0,{border_style},{outline},{th['shadow']},{align},{round(width * 0.06)},"
        f"{round(width * 0.06)},{margin_v},1\n" + extra_styles + "\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    chunks = build_chunks(words, vertical)
    line_limit = _line_limit(width, size, th["upper"])
    lines = []
    prim, act = ass_color(th["primary"]), ass_color(th["active"])
    dim = ass_color(th.get("dim", th["primary"]))
    blur = th.get("blur", 0)
    pop = th["pop"]
    if anim in ("none", "fade", "slide", "typewriter", "zoom"):
        pop = 100
    elif anim == "bounce":
        pop = 135
    px, py = width // 2, {2: height - margin_v, 5: height // 2, 8: margin_v}[align]
    for ci, ch in enumerate(chunks):
        texts = [_clean(w["w"]) for w in ch]
        if th["upper"]:
            texts = [t.upper() for t in texts]
        brk = _line_break(texts, line_limit)
        nxt = chunks[ci + 1][0]["s"] if ci + 1 < len(chunks) else None
        hold_end = ch[-1]["e"] + 0.28
        if nxt is not None:
            hold_end = min(hold_end, nxt)
        for i, w in enumerate(ch):
            t0 = w["s"] if i else max(0.0, w["s"] - 0.04)
            t1 = ch[i + 1]["s"] if i + 1 < len(ch) else hold_end
            if t1 - t0 < 0.05:
                t1 = t0 + 0.05
            parts = []
            for j, tx in enumerate(texts):
                if anim == "typewriter" and j > i:
                    continue
                if th["mode"] == "progressive":
                    col = act if j == i else (prim if j < i else dim)
                else:
                    col = act if j == i else prim
                if j == i and anim == "bounce":
                    tag = (f"{{\\1c{col}\\fscx60\\fscy60\\t(0,110,\\fscx{pop}\\fscy{pop})"
                           f"\\t(110,220,\\fscx100\\fscy100)}}")
                    tail = "{\\fscx100\\fscy100}"
                elif j == i and pop != 100:
                    tag = (f"{{\\1c{col}\\fscx{pop - 22}\\fscy{pop - 22}"
                           f"\\t(0,90,\\fscx{pop}\\fscy{pop})}}")
                    tail = "{\\fscx100\\fscy100}"
                elif j == i:
                    tag, tail = f"{{\\1c{col}}}", ""
                else:
                    tag, tail = f"{{\\1c{col}\\fscx100\\fscy100}}", ""
                parts.append(tag + tx + tail + ("\\N" if j == brk else ""))
            body = " ".join(parts).replace("\\N ", "\\N")
            pre = "{\\fad(60,0)" + (f"\\blur{blur}" if blur else "") + "}" if i == 0 else (f"{{\\blur{blur}}}" if blur else "")
            if anim == "fade" and i == 0:
                pre = "{\\fad(160,60)" + (f"\\blur{blur}" if blur else "") + "}"
            elif anim == "slide" and i == 0:
                pre = f"{{\\fad(80,0)\\move({px},{py + 70},{px},{py},0,170)" + (f"\\blur{blur}" if blur else "") + "}"
            elif anim == "zoom" and i == 0:
                pre = "{\\fad(60,0)\\fscx70\\fscy70\\t(0,130,\\fscx100\\fscy100)" + (f"\\blur{blur}" if blur else "") + "}"
            lines.append(f"Dialogue: 0,{_t(t0)},{_t(t1)},Default,,0,0,0,,{pre}{body}")
    return head + "\n".join(lines + list(extra_events or [])) + "\n"


# ---------- editor de subtítulos ----------
def chunk_texts(words: list[dict], vertical: bool = True) -> list[dict]:
    return [{"start": ch[0]["s"], "end": ch[-1]["e"], "text": " ".join(_clean(w["w"]) for w in ch)}
            for ch in build_chunks(words, vertical)]


def apply_edited_texts(words: list[dict], texts: list[str], vertical: bool = True) -> list[dict]:
    """Sustituye el texto de cada bloque conservando los tiempos (si cambia el nº de palabras, las reparte)."""
    chunks = build_chunks(words, vertical)
    if len(texts) != len(chunks):
        raise ValueError("El número de bloques no coincide.")
    out = []
    for ch, new in zip(chunks, texts):
        toks = new.split()
        if not toks:
            continue
        if len(toks) == len(ch):
            out.extend({**w, "w": t} for w, t in zip(ch, toks))
            continue
        t0, t1 = ch[0]["s"], ch[-1]["e"]
        total = sum(len(t) + 1 for t in toks)
        cur = t0
        for t in toks:
            d = (t1 - t0) * (len(t) + 1) / total
            out.append({"w": t, "s": cur, "e": cur + d, "p": 1.0})
            cur += d
    return out
