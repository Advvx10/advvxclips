"""IA para títulos y puntaje viral.

Proveedores:
  auto     Ollama si está activo (gratis, ilimitado, local) y si no, motor integrado.
  ollama   Ollama local (gratis e ilimitado). Requiere instalar Ollama y descargar un modelo.
  openai   Cualquier API compatible con OpenAI. Hay planes gratuitos con límites diarios (Groq, Gemini, OpenRouter).
  builtin  Solo el motor integrado (highlight.py): sin IA, sin red.

La IA nunca inventa tiempos: solo recibe fragmentos ya recortados por highlight.py y devuelve
puntaje + título + motivo. Si falla (sin red, cuota agotada, JSON roto), se conserva el puntaje integrado.
"""
import json
import re
import time
import urllib.error
import urllib.request

from . import highlight


class AIError(Exception):
    pass


def _http(method: str, url: str, body=None, headers=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            payload = {}
        return e.code, payload
    except Exception as e:  # noqa: BLE001
        raise AIError(f"Sin conexión ({type(e).__name__}).") from e


# ---------------- Ollama ----------------
def ollama_status(s: dict) -> dict:
    url = s["ollama_url"].rstrip("/")
    try:
        code, d = _http("GET", url + "/api/tags", timeout=3)
    except AIError:
        return {"running": False, "models": [], "installed": False}
    models = [m.get("name", "") for m in d.get("models", [])] if code == 200 else []
    want = s["ollama_model"]
    ok = any(m == want or (":" not in want and m.startswith(want + ":")) for m in models)
    return {"running": code == 200, "models": models, "installed": ok}


def resolve(s: dict) -> str:
    p = s.get("ai_provider", "auto")
    if p == "openai" and s.get("openai_key"):
        return "openai"
    if p == "ollama":
        return "ollama"
    if p == "builtin":
        return "builtin"
    st = ollama_status(s)
    return "ollama" if st["running"] and st["installed"] else "builtin"


def chat(s: dict, system: str, user: str, json_mode=True, max_tokens=1500, provider=None) -> str:
    provider = provider or resolve(s)
    if provider == "ollama":
        body = {"model": s["ollama_model"], "stream": False,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "options": {"temperature": 0.25, "num_ctx": 8192, "num_predict": max_tokens}}
        if json_mode:
            body["format"] = "json"
        code, d = _http("POST", s["ollama_url"].rstrip("/") + "/api/chat", body, timeout=900)
        if code == 404:
            raise AIError(f"Modelo '{s['ollama_model']}' no descargado en Ollama.")
        if code >= 400:
            raise AIError(f"Ollama {code}: {str(d)[:160]}")
        return d.get("message", {}).get("content", "")
    if provider == "openai":
        body = {"model": s["openai_model"], "max_tokens": max_tokens, "temperature": 0.25,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        hdr = {"Authorization": f"Bearer {s['openai_key']}"}
        for attempt in range(3):
            code, d = _http("POST", s["openai_base_url"].rstrip("/") + "/chat/completions", body, hdr, timeout=120)
            if code == 429 and attempt < 2:   # cuota del plan gratuito: espera y reintenta
                time.sleep(6 * (attempt + 1))
                continue
            break
        if code >= 400:
            msg = (d.get("error") or {}).get("message") if isinstance(d.get("error"), dict) else d
            raise AIError(f"API {code}: {str(msg)[:160]}")
        return d["choices"][0]["message"]["content"]
    raise AIError("Sin proveedor de IA.")


def extract_json(text: str):
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise AIError("La IA no devolvió JSON.")
    text = text[min(starts):]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        for close in ("}", "]"):
            end = text.rfind(close)
            while end > 0:
                try:
                    return json.loads(text[:end + 1])
                except json.JSONDecodeError:
                    end = text.rfind(close, 0, end)
        raise AIError("JSON de la IA no válido.")


SYS = """Eres un editor experto de clips virales para TikTok, Reels y Shorts (streamers, podcasts, entrevistas).
Recibes fragmentos numerados de una transcripción. Para CADA fragmento devuelve:
- "score": 0-100, potencial viral. Sé exigente: 85+ solo si es excepcional; 60-80 bueno; <50 aburrido o dependiente de contexto.
  Premia: gancho en los primeros 3 segundos, emoción/conflicto/humor/sorpresa, una historia o idea completa, valor útil, cierre claro.
  Penaliza: relleno, saludos, contexto necesario que no está, ideas cortadas.
- "title": título de 4 a 9 palabras, en el MISMO idioma del fragmento, que DIGA DE QUÉ TRATA el clip
  (su tema, sus pasos, su historia), con palabras que realmente se dicen en el fragmento. Es el tema
  del clip, NO una etiqueta del tipo de momento: NUNCA escribas "Se pelean en pleno directo",
  "Discusión fuerte en el directo", "Gritos en pleno directo" ni ninguna etiqueta parecida de
  pelea/discusión/risa/gritos. Nunca inventes nada que no se diga en el fragmento, nunca prometas
  algo que el clip no muestra (sin clickbait). Sin comillas, sin emojis, sin "clip", sin punto final.
  Ejemplos buenos: "Así se prepara la pasta" (un clip que explica los pasos de una pasta),
  "No aguantó la risa con esa historia".
  Ejemplos malos: "Se pelean en pleno directo" (etiqueta de momento, no dice de qué trata),
  "Discusión fuerte en el directo" (etiqueta de momento), "Jajaja mira cómo se puso" (no resume nada),
  "No vas a creer lo que pasó" (clickbait).
- "summary": una línea (máx 140 caracteres) que diga LO MISMO que el título pero con otras palabras,
  también solo con lo que realmente se dice en el fragmento. Sin comillas ni emojis.
- "reason": una frase corta del porqué.
Responde SOLO JSON: {"items":[{"id":1,"score":72,"title":"...","summary":"...","reason":"..."}]}"""


_LAUGH_RE = re.compile(r"(?i)^[\s]*(?:ja|je|ji|jo|ju|ha|he)+[\s.!…]*$")


def _is_laugh_only(text: str) -> bool:
    """True si el texto es solo una risa (jajaja, jejeje, hahaha...)."""
    return bool(text and _LAUGH_RE.match(text.strip()))


def _builtin_title(c: dict) -> str:
    """Título de resumen sin IA: highlight.make_summary_title si existe, si no make_title."""
    fn = getattr(highlight, "make_summary_title", None)
    if callable(fn):
        return fn(c.get("text", ""), c.get("moment", ""))
    return highlight.make_title(c.get("text", ""))


def _starts_with_laugh(text: str) -> bool:
    """True si la primera palabra es una risa (Jajaja, mira...)."""
    first = (text or "").strip().split(" ", 1)[0].strip(",.!…")
    return bool(first and re.fullmatch(r"(?i)(?:ja|je|ji|jo|ju|ha|he)+", first))


def _first_content_sentence(text: str, max_len: int = 110) -> str:
    """Primera frase con contenido del fragmento (salta risas y frases de menos de 3 palabras)."""
    for part in re.split(r"[.!?¡¿\n]+", text or ""):
        s = " ".join(part.split()).strip(" ,;:—-")
        if len(s.split()) < 3 or _is_laugh_only(s) or _starts_with_laugh(s):
            continue
        if len(s) > max_len:
            cut = s[:max_len].rsplit(" ", 1)[0] or s[:max_len]
            return cut.rstrip(" ,;:") + "…"
        return s
    return ""


# «De qué trata el video» (v3.1.4) -> tipo que entiende la IA, como en
# el SamuelClip del amigo (interview / narration / general).
CONTENT_TYPE_MAP = {"entrevista": "interview", "podcast": "interview", "resumen": "narration",
                    "pelicula": "general", "gaming": "general"}
CONTENT_TYPE_HINT = {
    "interview": "Es una entrevista o podcast: premia los intercambios entre personas, las discusiones y las respuestas con historia.",
    "narration": "Es un resumen/narración: busca un suceso con causa y desenlace, sin exigir diálogo entre personas.",
    "general": "",
}


def rerank(s: dict, cands: list[dict], info: dict | None = None, batch: int = 4) -> list[dict]:
    """Añade a cada candidato: ai_score/title/reason. Devuelve la lista (misma longitud)."""
    info = info if info is not None else {}
    provider = resolve(s)
    for c in cands:
        c["title"] = _builtin_title(c)
        c["summary"] = c.get("summary") or _first_content_sentence(c.get("text", ""))
        c["final"] = c["score"]
    if provider == "builtin" or not cands:
        info["engine"] = ("motor integrado (sin IA: los títulos son plantillas, no palabras del clip — "
                          "pon tu clave gratis de Groq en Ajustes > IA para títulos reales)")
        return cands
    done, err = 0, ""
    ct = CONTENT_TYPE_MAP.get(str(s.get("content_type", "general") or "general").lower(), "general")
    ct_hint = CONTENT_TYPE_HINT.get(ct, "")
    for i in range(0, len(cands), batch):
        part = cands[i:i + batch]
        user = ("Tipo de contenido: " + ct + (". " + ct_hint if ct_hint else "") + "\n\n"
                if ct != "general" or ct_hint else "") + "Fragmentos:\n" + "\n\n".join(
            f"[{j + 1}] ({c['end'] - c['start']:.0f}s)\n{c['text'][:900]}" for j, c in enumerate(part))
        try:
            items = extract_json(chat(s, SYS, user, provider=provider))
            items = items.get("items", items) if isinstance(items, dict) else items
            for it in items:
                try:
                    c = part[int(it["id"]) - 1]
                    llm_score = max(0, min(100, float(it["score"])))
                except (KeyError, ValueError, TypeError, IndexError):
                    continue
                c["ai_score"] = llm_score
                c["final"] = round(0.6 * llm_score + 0.4 * c["score"])
                t = str(it.get("title", "")).strip().strip('"“”«»').rstrip(".")
                if 4 <= len(t) <= 90 and len(t.split()) >= 3 and not _is_laugh_only(t):
                    c["title"] = t
                # si el título de la IA no sirve, queda el título de resumen integrado
                summary = str(it.get("summary") or "").strip()
                if summary:
                    c["summary"] = summary[:140]
                if it.get("reason"):
                    c["reason"] = str(it["reason"])[:200]
                done += 1
        except AIError as e:
            err = str(e)
            if "no descargado" in err or "Sin conexión" in err or "API 4" in err:
                break  # no tiene sentido seguir llamando
    name = s["ollama_model"] if provider == "ollama" else s["openai_model"]
    if done:
        info["engine"] = f"IA {'local' if provider == 'ollama' else 'API'} · {name}"
    else:
        info["engine"] = f"motor integrado (la IA no respondió: {err or 'sin resultados'})"
    for c in cands:
        c["score"] = int(c["final"])
    return cands


def caption_text(s: dict, title: str, text: str) -> str:
    """Descripción corta para publicar (con respaldo local)."""
    try:
        if resolve(s) != "builtin":
            out = chat(s, "Escribes descripciones cortas (máx 2 líneas) con gancho para redes sociales, en el idioma "
                          "del texto, sin hashtags ni comillas. Responde SOLO el texto.",
                       f"Título: {title}\nContenido: {text[:1200]}", json_mode=False, max_tokens=200).strip()
            if out:
                return out
    except AIError:
        pass
    return title


def status(s: dict) -> dict:
    o = ollama_status(s)
    p = resolve(s)
    return {"provider": p, "ollama": o, "model": s["ollama_model"], "configured": s.get("ai_provider", "auto"),
            "openai_ready": bool(s.get("openai_key")),
            "engine": {"ollama": f"Ollama · {s['ollama_model']}", "openai": f"API · {s['openai_model']}",
                       "builtin": "Motor integrado"}[p]}
