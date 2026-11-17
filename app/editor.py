"""Editor de video integrado.

Un proyecto del editor (`spec`) es un JSON con: segmentos (tramos de clips) en una línea de tiempo, subtítulos
(tema, animación, sincronía), música de fondo, overlays (imagen/texto), zooms y seguimientos (rostro/objeto).
El navegador previsualiza el resultado en tiempo real con la misma matemática que este módulo usa para el render
final con ffmpeg (por eso `geometry()` y la versión de JavaScript deben dar los mismos números).
"""
import difflib
import json
import math
import re
import shutil
import subprocess
import threading
import time
import unicodedata
import uuid
from pathlib import Path

from . import captions, config, db, media, transcribe

EDIT_DIR = config.DATA_DIR / "editor"
PROXY_DIR, ASSET_DIR, STITCH_DIR, EXPORT_DIR = (EDIT_DIR / n for n in ("proxy", "assets", "stitch", "export"))
for _d in (PROXY_DIR, ASSET_DIR, STITCH_DIR, EXPORT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

PROXY_PAD = 15.0          # segundos extra a cada lado del clip dentro del proxy (permite alargar el recorte)
MAX_SEGMENTS, MAX_OVERLAYS, MAX_ZOOMS = 40, 30, 60
HEX = re.compile(r"^[0-9A-Fa-f]{6}$")
ID = re.compile(r"^[A-Za-z0-9_:\-]{1,40}$")


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def num(v, default=0.0, lo=-1e9, hi=1e9) -> float:
    try:
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")):
            return default
        return clamp(f, lo, hi)
    except (TypeError, ValueError):
        return default


def uid(prefix="s") -> str:
    return prefix + uuid.uuid4().hex[:6]


# ====================================================================== fuentes y proxies
_dur_cache: dict = {}


def _probe_cached(path: Path) -> dict:
    key = (str(path), path.stat().st_mtime_ns)
    if key not in _dur_cache:
        _dur_cache[key] = media.probe(path)
    return _dur_cache[key]


def clip_source(clip: dict) -> dict:
    """Ubica el video ORIGINAL de un clip (sin subtítulos grabados).
    {path, base, fallback, w, h, duration, has_audio}: tiempo_de_archivo = tiempo_origen - base."""
    src = json.loads(clip.get("source_path") or "{}")
    inputs = [Path(p) for p in src.get("inputs", [])]
    if inputs and all(p.exists() for p in inputs):
        if src.get("concat") and len(inputs) > 1:
            out = STITCH_DIR / f"{clip['id']}.ts"
            if not out.exists():
                lst = STITCH_DIR / f"{clip['id']}.txt"
                lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in inputs), encoding="utf-8")
                media.run([config.ffmpeg(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                           "-c", "copy", str(out)])
                lst.unlink(missing_ok=True)
            path = out
        else:
            path = inputs[0]
        pi = _probe_cached(path)
        return {"path": path, "base": float(src.get("offset", 0.0)), "fallback": False, "w": pi["width"],
                "h": pi["height"], "duration": pi["duration"], "has_audio": pi["has_audio"]}
    p = Path(clip.get("path") or "")
    if p.exists():       # el original ya no está: se edita el clip renderizado (con sus subtítulos grabados)
        pi = _probe_cached(p)
        return {"path": p, "base": float(clip["start"]), "fallback": True, "w": pi["width"], "h": pi["height"],
                "duration": pi["duration"], "has_audio": pi["has_audio"]}
    raise media.MediaError(f"El clip {clip['id']} ya no tiene video en disco.")


def proxy_format() -> tuple[str, list[str]]:
    import os
    if os.environ.get("TBX_PROXY_FORMAT") == "webm":     # (solo pruebas: el Chromium de pruebas no reproduce H.264)
        return "webm", ["-c:v", "libvpx", "-b:v", "900k", "-deadline", "realtime", "-cpu-used", "8", "-g", "12",
                        "-c:a", "libvorbis", "-b:a", "80k"]
    return "mp4", ["-c:v", "libx264", "-preset", "veryfast", "-crf", "27", "-g", "12", "-keyint_min", "12",
                   "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart"]


_proxy_locks: dict[int, threading.Lock] = {}


def proxy_info(clip_id: int) -> dict | None:
    meta = PROXY_DIR / f"{clip_id}.json"
    if not meta.exists():
        return None
    d = json.loads(meta.read_text(encoding="utf-8"))
    return d if Path(d["path"]).exists() else None


def ensure_proxy(clip: dict) -> dict:
    """Crea (una vez) la copia ligera del tramo del clip que el navegador reproduce en el editor."""
    lock = _proxy_locks.setdefault(clip["id"], threading.Lock())
    with lock:
        info = proxy_info(clip["id"])
        if info:
            return info
        s = clip_source(clip)
        lo = max(s["base"], clip["start"] - PROXY_PAD)
        hi = min(s["base"] + s["duration"], clip["end"] + PROXY_PAD)
        if s["fallback"]:
            lo, hi = clip["start"], clip["end"]
        ext, venc = proxy_format()
        out = PROXY_DIR / f"{clip['id']}.{ext}"
        cmd = [config.ffmpeg(), "-y", "-loglevel", "error", "-ss", f"{max(0.0, lo - s['base']):.3f}", "-i", str(s["path"]),
               "-t", f"{hi - lo:.3f}", "-map", "0:v:0", "-map", "0:a:0?", "-vf", "scale=-2:'min(480,ih)',fps=30",
               *venc, str(out)]
        media.run(cmd, timeout=1800)
        pi = media.probe(out)
        info = {"path": str(out), "base": lo, "duration": pi["duration"], "w": pi["width"], "h": pi["height"],
                "has_audio": pi["has_audio"], "fallback": s["fallback"], "src_w": s["w"], "src_h": s["h"]}
        (PROXY_DIR / f"{clip['id']}.json").write_text(json.dumps(info), encoding="utf-8")
        return info


def job_words(job_id: int) -> list[dict]:
    f = config.JOBS_DIR / str(job_id) / "words.json"
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
    except (OSError, json.JSONDecodeError):
        return []


# ====================================================================== modelo del proyecto
def default_spec() -> dict:
    return {"v": 1, "layout": "blur", "segments": [],
            "subtitles": {"enabled": True, "theme": "impact", "position": "low", "anim": "pop", "size": 1.0, "offset": 0.0},
            "music": None, "overlays": [], "zooms": [], "tracks": {}}


def segment_from_clip(clip: dict) -> dict:
    """Segmento inicial = el clip tal cual, con sus palabras en tiempo de origen (+ contexto del video completo)."""
    words = [{"w": w["w"], "s": round(clip["start"] + w["s"], 3), "e": round(clip["start"] + w["e"], 3)}
             for w in json.loads(clip.get("words") or "[]")]
    try:
        fallback = clip_source(clip)["fallback"]
    except media.MediaError:
        fallback = True
    if not fallback:     # palabras del video completo alrededor del clip: permiten alargar el recorte con subtítulos
        for w in job_words(clip["job_id"]):
            if (clip["start"] - 20 <= w["s"] and w["e"] <= clip["end"] + 20
                    and not (clip["start"] - 0.05 <= w["s"] and w["e"] <= clip["end"] + 0.05)):
                words.append({"w": w["w"], "s": round(w["s"], 3), "e": round(w["e"], 3)})
    words.sort(key=lambda x: x["s"])
    return {"uid": uid("s"), "clip_id": clip["id"], "src_in": round(clip["start"], 3), "src_out": round(clip["end"], 3),
            "volume": 1.0, "words": words, "title": clip.get("title", "")}


def sanitize(spec: dict) -> dict:
    """Valida y limpia el JSON que llega del navegador: todo lo que acaba en ffmpeg pasa por aquí."""
    d = default_spec()
    if not isinstance(spec, dict):
        return d
    d["layout"] = spec.get("layout") if spec.get("layout") in ("blur", "fill", "original", "gaming") else "blur"
    segs, seen = [], set()
    for s in (spec.get("segments") or [])[:MAX_SEGMENTS]:
        try:
            cid = int(s["clip_id"])
        except (KeyError, TypeError, ValueError):
            continue
        u = s.get("uid") if isinstance(s.get("uid"), str) and ID.match(s.get("uid", "")) and s["uid"] not in seen else uid("s")
        seen.add(u)
        a, b = num(s.get("src_in"), 0, 0, 1e7), num(s.get("src_out"), 1, 0, 1e7)
        if b - a < 0.2:
            b = a + 0.2
        words = []
        for w in (s.get("words") or [])[:6000]:
            t = str(w.get("w", "")).replace("\n", " ").strip()[:60]
            if t:
                ws = num(w.get("s"), 0, 0, 1e7)
                words.append({"w": t, "s": round(ws, 3), "e": round(max(num(w.get("e"), ws + 0.1, 0, 1e7), ws + 0.04), 3)})
        segs.append({"uid": u, "clip_id": cid, "src_in": round(a, 3), "src_out": round(b, 3),
                     "volume": round(num(s.get("volume"), 1.0, 0, 3), 3), "words": words,
                     "title": str(s.get("title", ""))[:100]})
    d["segments"] = segs
    sub = spec.get("subtitles") or {}
    d["subtitles"] = {"enabled": bool(sub.get("enabled", True)),
                      "theme": sub.get("theme") if sub.get("theme") in captions.THEMES else "impact",
                      "position": sub.get("position") if sub.get("position") in ("low", "center", "top") else "low",
                      "anim": sub.get("anim") if sub.get("anim") in captions.ANIMATIONS else "pop",
                      "size": round(num(sub.get("size"), 1.0, 0.6, 1.8), 3),
                      "offset": round(num(sub.get("offset"), 0.0, -10, 10), 3)}
    m = spec.get("music")
    if isinstance(m, dict) and isinstance(m.get("asset"), str) and ID.match(m["asset"]) and get_asset(m["asset"]):
        d["music"] = {"asset": m["asset"], "volume": round(num(m.get("volume"), 0.25, 0, 2), 3),
                      "start": round(num(m.get("start"), 0, 0, 1e5), 3), "loop": bool(m.get("loop", True)),
                      "fade_in": round(num(m.get("fade_in"), 0.5, 0, 30), 2), "fade_out": round(num(m.get("fade_out"), 1.5, 0, 30), 2),
                      "duck": bool(m.get("duck", True)), "offset": round(num(m.get("offset"), 0, 0, 1e5), 3)}
    ovs = []
    for o in (spec.get("overlays") or [])[:MAX_OVERLAYS]:
        typ = o.get("type")
        end = o.get("end")
        base = {"id": o.get("id") if isinstance(o.get("id"), str) and ID.match(o.get("id", "")) else uid("o"), "type": typ,
                "x": round(num(o.get("x"), 0.5, -0.5, 1.5), 4), "y": round(num(o.get("y"), 0.5, -0.5, 1.5), 4),
                "start": round(num(o.get("start"), 0, 0, 1e5), 3),
                "end": None if end in (None, "") else round(num(end, 0, 0, 1e5), 3), "opacity": round(num(o.get("opacity"), 1, 0, 1), 3)}
        if typ == "image" and isinstance(o.get("asset"), str) and ID.match(o["asset"]) and get_asset(o["asset"]):
            ovs.append({**base, "asset": o["asset"], "w": round(num(o.get("w"), 0.3, 0.02, 2), 4)})
        elif typ == "text":
            text = str(o.get("text", "")).replace("\r", "")[:120]
            if text.strip():
                ovs.append({**base, "text": text, "size": round(num(o.get("size"), 0.04, 0.01, 0.2), 4),
                            "color": o.get("color") if HEX.match(str(o.get("color", ""))) else "FFFFFF",
                            "bg": bool(o.get("bg", False))})
    d["overlays"] = ovs
    zs = []
    for z in (spec.get("zooms") or [])[:MAX_ZOOMS]:
        a = num(z.get("start"), 0, 0, 1e5)
        b = num(z.get("end"), a + 1, 0, 1e5)
        if b - a < 0.2:
            b = a + 0.2
        f = z.get("focus") or {}
        mode = f.get("mode") if f.get("mode") in ("manual", "face", "track") else "manual"
        focus = {"mode": mode, "x": round(num(f.get("x"), 0.5, 0, 1), 4), "y": round(num(f.get("y"), 0.5, 0, 1), 4)}
        if mode == "track" and isinstance(f.get("track"), str) and ID.match(f["track"]):
            focus["track"] = f["track"]
        elif mode == "track":
            focus["mode"] = "manual"
        zs.append({"id": z.get("id") if isinstance(z.get("id"), str) and ID.match(z.get("id", "")) else uid("z"),
                   "start": round(a, 3), "end": round(b, 3), "scale": round(num(z.get("scale"), 1.5, 1.0, 4.0), 3),
                   "ease": round(num(z.get("ease"), 0.35, 0.0, 3.0), 3), "focus": focus})
    d["zooms"] = sorted(zs, key=lambda z: z["start"])
    tr = {}
    for k, t in list((spec.get("tracks") or {}).items())[:40]:
        if not ID.match(str(k)) or not isinstance(t, dict):
            continue
        samples = []
        for p in (t.get("samples") or [])[:6000]:
            try:
                samples.append([round(float(p[0]), 3), round(clamp(float(p[1]), 0, 1), 4), round(clamp(float(p[2]), 0, 1), 4)])
            except (TypeError, ValueError, IndexError):
                pass
        if samples:
            tr[k] = {"kind": "face" if t.get("kind") == "face" else "object", "seg": str(t.get("seg", ""))[:40],
                     "label": str(t.get("label", ""))[:40], "samples": samples}
    d["tracks"] = tr
    return d


# ====================================================================== línea de tiempo
def seg_dur(seg: dict) -> float:
    return max(0.2, seg["src_out"] - seg["src_in"])


def timeline(spec: dict) -> list[tuple[dict, float, float]]:
    out, t = [], 0.0
    for s in spec["segments"]:
        d = seg_dur(s)
        out.append((s, t, t + d))
        t += d
    return out


def total_duration(spec: dict) -> float:
    return sum(seg_dur(s) for s in spec["segments"])


def locate(spec: dict, t: float):
    tl = timeline(spec)
    for s, a, b in tl:
        if t < b:
            return s, a
    return (tl[-1][0], tl[-1][1]) if tl else (None, 0.0)


def timeline_words(spec: dict) -> list[dict]:
    """Palabras de todos los segmentos en tiempo de línea de tiempo (recortes y desfase de sincronía aplicados)."""
    off = spec["subtitles"]["offset"]
    out = []
    for seg, t0, t1 in timeline(spec):
        for w in seg["words"]:
            mid = (w["s"] + w["e"]) / 2
            if not (seg["src_in"] <= mid < seg["src_out"]):
                continue
            s = t0 + max(0.0, w["s"] - seg["src_in"]) + off
            e = t0 + min(w["e"] - seg["src_in"], t1 - t0) + off
            if e <= 0:
                continue
            s = max(0.0, s)
            out.append({"w": w["w"], "s": round(s, 3), "e": round(max(e, s + 0.05), 3)})
    out.sort(key=lambda x: x["s"])
    return out


# ====================================================================== reencuadre: zoom + seguimiento
def smoothstep(x: float) -> float:
    x = clamp(x, 0.0, 1.0)
    return x * x * (3 - 2 * x)


def track_at(samples: list, t: float) -> tuple[float, float]:
    if not samples:
        return 0.5, 0.5
    if t <= samples[0][0]:
        return samples[0][1], samples[0][2]
    if t >= samples[-1][0]:
        return samples[-1][1], samples[-1][2]
    lo, hi = 0, len(samples) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if samples[mid][0] <= t:
            lo = mid
        else:
            hi = mid
    a, b = samples[lo], samples[hi]
    f = (t - a[0]) / (b[0] - a[0]) if b[0] > a[0] else 0
    return a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f


def focus_at(spec: dict, seg: dict, local_t: float, tl_t: float) -> tuple[float, float, float]:
    """(zoom, fx, fy) en un instante. Fuera de un zoom: (1, .5, .5)."""
    zoom = None
    for z in spec["zooms"]:
        if z["start"] <= tl_t <= z["end"]:
            zoom = z
    if zoom is None:
        return 1.0, 0.5, 0.5
    e = max(0.01, min(zoom["ease"], (zoom["end"] - zoom["start"]) / 2))
    env = smoothstep((tl_t - zoom["start"]) / e) * smoothstep((zoom["end"] - tl_t) / e)
    f = zoom["focus"]
    px, py = f["x"], f["y"]
    if f["mode"] in ("face", "track"):
        tid = f"face:{seg['uid']}" if f["mode"] == "face" else f.get("track", "")
        tr = spec["tracks"].get(tid)
        if tr and (f["mode"] == "face" or tr["seg"] == seg["uid"]):
            px, py = track_at(tr["samples"], seg["src_in"] + local_t)
        else:
            px, py = 0.5, 0.5
    return 1.0 + (zoom["scale"] - 1.0) * env, 0.5 + (px - 0.5) * env, 0.5 + (py - 0.5) * env


def base_scale(layout: str, W: int, H: int, iw: int, ih: int) -> float:
    """Factor que lleva el video al lienzo: 'blur' = ajustar (cabe entero), el resto = cubrir."""
    return min(W / iw, H / ih) if layout == "blur" else max(W / iw, H / ih)


def axis_offset(canvas: int, size: float, f: float) -> float:
    if size >= canvas:
        return max(canvas - size, min(0.0, canvas / 2 - f * size))
    return (canvas - size) / 2


def canvas_size(layout: str, iw: int, ih: int, quality: str = "final") -> tuple[int, int]:
    if layout == "original":
        cap = 1920 if quality == "final" else 960
        w = min(iw or 1920, cap) // 2 * 2
        return w, (round(w * (ih or 1080) / (iw or 1920)) // 2) * 2
    return (1080, 1920) if quality == "final" else (540, 960)


def geometry(spec: dict, seg: dict, local_t: float, tl_t: float, W: int, H: int, iw: int, ih: int) -> dict:
    z, fx, fy = focus_at(spec, seg, local_t, tl_t)
    k = base_scale(spec["layout"], W, H, iw, ih)
    ow, oh = iw * k * z, ih * k * z
    return {"z": z, "fx": fx, "fy": fy, "ow": ow, "oh": oh, "ox": axis_offset(W, ow, fx), "oy": axis_offset(H, oh, fy)}


# ---- curvas -> expresiones de ffmpeg
def rdp(points: list[tuple[float, float]], tol: float) -> list[tuple[float, float]]:
    if len(points) < 3:
        return points
    keep = {0, len(points) - 1}
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        (ta, va), (tb, vb) = points[a], points[b]
        worst, idx = 0.0, -1
        for i in range(a + 1, b):
            t, v = points[i]
            interp = va + (vb - va) * ((t - ta) / (tb - ta) if tb > ta else 0)
            if abs(v - interp) > worst:
                worst, idx = abs(v - interp), i
        if worst > tol and idx > 0:
            keep.add(idx)
            stack += [(a, idx), (idx, b)]
    return [points[i] for i in sorted(keep)]


def _f(x: float) -> str:
    return f"{x:.5f}".rstrip("0").rstrip(".") if "." in f"{x:.5f}" else f"{x:.5f}"


def pwl(points: list[tuple[float, float]]) -> str:
    """Expresión de ffmpeg (variable t) lineal por tramos. Constante si la curva no cambia."""
    pts = []
    for t, v in points:
        if not pts or t > pts[-1][0] + 1e-6:
            pts.append((t, v))
    if len(pts) == 1 or max(v for _, v in pts) - min(v for _, v in pts) < 1e-6:
        return _f(pts[0][1])
    parts = [f"lt(t,{_f(pts[0][0])})*{_f(pts[0][1])}"]
    for (ta, va), (tb, vb) in zip(pts, pts[1:]):
        slope = (vb - va) / (tb - ta)
        parts.append(f"gte(t,{_f(ta)})*lt(t,{_f(tb)})*({_f(va)}+({_f(slope)})*(t-{_f(ta)}))")
    parts.append(f"gte(t,{_f(pts[-1][0])})*{_f(pts[-1][1])}")
    return "(" + "+".join(parts) + ")"


def curves_for_segment(spec: dict, seg: dict, tl0: float, step: float = 0.1, max_points: int = 70):
    """Muestrea zoom/foco en tiempo LOCAL del segmento y los reduce a pocos puntos clave."""
    dur = seg_dur(seg)
    n = max(1, int(math.ceil(dur / step)))
    ts = [min(dur, i * step) for i in range(n + 1)]
    zs, fxs, fys = [], [], []
    for t in ts:
        z, fx, fy = focus_at(spec, seg, t, tl0 + t)
        zs.append((t, z))
        fxs.append((t, fx))
        fys.append((t, fy))
    out = []
    for pts, tol in ((zs, 0.004), (fxs, 0.0015), (fys, 0.0015)):
        cur = tol
        r = rdp(pts, cur)
        while len(r) > max_points:      # tope de tamaño de la expresión
            cur *= 1.6
            r = rdp(pts, cur)
        out.append(r)
    return out


# ====================================================================== assets (música / imágenes)
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def get_asset(aid: str) -> dict | None:
    r = db.q1("SELECT * FROM editor_assets WHERE id=?", (aid,))
    return r if r and Path(r["path"]).exists() else None


def add_asset(data_path: Path, name: str) -> dict:
    """Registra un archivo ya guardado en ASSET_DIR (la subida la hace el servidor)."""
    ext = Path(name).suffix.lower()
    kind = "audio" if ext in AUDIO_EXT else "image" if ext in IMAGE_EXT else ""
    if not kind:
        data_path.unlink(missing_ok=True)
        raise media.MediaError("Formato no soportado. Audio: mp3, wav, m4a, ogg, flac · Imagen: png, jpg, webp.")
    if kind == "image":      # ffprobe acepta hasta texto plano como "video": se valida la firma real del archivo
        head = data_path.read_bytes()[:12]
        ok = (head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8\xff") or head.startswith(b"GIF8")
              or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"))
        if not ok:
            data_path.unlink(missing_ok=True)
            raise media.MediaError("El archivo no es una imagen válida (PNG, JPG, WEBP o GIF).")
    aid = uuid.uuid4().hex[:12]
    final = ASSET_DIR / f"{aid}{ext}"
    shutil.move(str(data_path), final)
    try:
        pi = media.probe(final)
    except media.MediaError:
        final.unlink(missing_ok=True)
        raise media.MediaError("El archivo está dañado o no se pudo leer.")
    if (kind == "audio" and not pi["has_audio"]) or (kind == "image" and not pi["has_video"]):
        final.unlink(missing_ok=True)
        raise media.MediaError("El archivo no contiene " + ("audio." if kind == "audio" else "imagen."))
    db.run("INSERT INTO editor_assets(id,name,kind,path,duration,created_at) VALUES(?,?,?,?,?,?)",
           (aid, Path(name).name[:80], kind, str(final), pi["duration"] if kind == "audio" else 0, db.now()))
    return {"id": aid, "name": Path(name).name[:80], "kind": kind, "duration": pi["duration"] if kind == "audio" else 0}


# ====================================================================== render
_cap_cache: dict = {}


def ffmpeg_has_option(flt: str, opt: str) -> bool:
    key = (config.ffmpeg(), flt, opt)
    if key not in _cap_cache:
        try:
            out = subprocess.run([config.ffmpeg(), "-hide_banner", "-h", f"filter={flt}"], capture_output=True,
                                 timeout=20, **config.NOWIN)
            _cap_cache[key] = opt in (out.stdout + out.stderr).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            _cap_cache[key] = False
    return _cap_cache[key]


def fit_to_sources(spec: dict) -> dict:
    """Recorta los segmentos al rango realmente disponible del video original."""
    for seg in spec["segments"]:
        clip = db.q1("SELECT * FROM clips WHERE id=?", (seg["clip_id"],))
        if not clip:
            raise media.MediaError("Un clip del proyecto fue eliminado.")
        s = clip_source(clip)
        lo, hi = s["base"], s["base"] + s["duration"]
        seg["src_in"] = round(clamp(seg["src_in"], lo, hi - 0.3), 3)
        seg["src_out"] = round(clamp(seg["src_out"], seg["src_in"] + 0.2, hi), 3)
    return spec


def _ass_text_overlays(spec: dict, W: int, H: int, total: float) -> tuple[str, list[str]]:
    styles = ("Style: Ovl,Poppins,60,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0.5,0,1,3,1,5,0,0,0,1\n"
              "Style: OvlBox,Poppins,60,&H00FFFFFF,&H00FFFFFF,&H00101014,&H80000000,-1,0,0,0,100,100,0.5,0,3,14,0,5,0,0,0,1\n")
    events = []
    for o in spec["overlays"]:
        if o["type"] != "text":
            continue
        end = o["end"] if o["end"] is not None else total
        if end <= o["start"]:
            continue
        fs = max(8, round(o["size"] * H))
        alpha = f"{int((1 - o['opacity']) * 255):02X}"
        col = captions.ass_color(o["color"])
        text = captions._clean(o["text"]).replace("  ", " ")
        text = "\\N".join(captions._clean(p) for p in o["text"].split("\n"))
        tags = (f"{{\\an5\\pos({round(o['x'] * W)},{round(o['y'] * H)})\\fs{fs}\\1c{col}\\1a&H{alpha}&"
                f"\\3a&H{alpha}&\\bord{max(1, round(fs * 0.07))}}}")
        events.append(f"Dialogue: 1,{captions._t(o['start'])},{captions._t(end)},{'OvlBox' if o['bg'] else 'Ovl'},,0,0,0,,{tags}{text}")
    return styles, events


def build_render(spec: dict, quality: str, settings: dict, work: Path) -> dict:
    """Construye entradas, grafo de filtros y opciones de salida. Devuelve todo lo necesario para ejecutar ffmpeg."""
    segs = timeline(spec)
    if not segs:
        raise media.MediaError("El proyecto no tiene clips.")
    layout = spec["layout"]
    infos = {}
    for seg, _, _ in segs:
        clip = db.q1("SELECT * FROM clips WHERE id=?", (seg["clip_id"],))
        if not clip:
            raise media.MediaError("Un clip del proyecto fue eliminado.")
        infos[seg["uid"]] = clip_source(clip)
    first = infos[segs[0][0]["uid"]]
    W, H = canvas_size(layout, first["w"], first["h"], quality)
    total = total_duration(spec)
    inputs: list[str] = []
    g: list[str] = []
    vl, al = [], []
    for i, (seg, t0, t1) in enumerate(segs):
        info = infos[seg["uid"]]
        dur = seg_dur(seg)
        ft = max(0.0, seg["src_in"] - info["base"])
        seek = max(0.0, ft - 2.0)
        pre = ft - seek
        inputs += ["-ss", f"{seek:.3f}", "-t", f"{pre + dur + 0.6:.3f}", "-i", str(info["path"])]
        zc, fxc, fyc = curves_for_segment(spec, seg, t0)
        k = base_scale(layout, W, H, info["w"], info["h"])
        zx = pwl(zc)
        w_expr = f"'trunc(iw*{_f(k)}*{zx}/2)*2'"
        h_expr = f"'trunc(ih*{_f(k)}*{zx}/2)*2'"
        ox = f"'if(gte(w,W),max(W-w,min(0,W/2-{pwl(fxc)}*w)),(W-w)/2)'"
        oy = f"'if(gte(h,H),max(H-h,min(0,H/2-{pwl(fyc)}*h)),(H-h)/2)'"
        head = f"[{i}:v]trim=start={pre:.3f}:duration={dur:.3f},setpts=PTS-STARTPTS,fps=30"
        if layout == "blur":
            g.append(f"{head},split=2[a{i}][b{i}]")
            g.append(f"[a{i}]scale={W // 4 // 2 * 2}:{H // 4 // 2 * 2}:force_original_aspect_ratio=increase,"
                     f"crop={W // 4 // 2 * 2}:{H // 4 // 2 * 2},boxblur=10:2,scale={W}:{H},eq=brightness=-0.08[bg{i}]")
            src_lbl = f"b{i}"
        else:
            g.append(f"{head}[b{i}]")
            g.append(f"color=c=black:s={W}x{H}:r=30[bg{i}]")
            src_lbl = f"b{i}"
        g.append(f"[{src_lbl}]scale=w={w_expr}:h={h_expr}:eval=frame[fg{i}]")
        g.append(f"[bg{i}][fg{i}]overlay=x={ox}:y={oy}:eval=frame:shortest=1,setsar=1,format=yuv420p[v{i}]")
        vol = _f(seg["volume"])
        if info["has_audio"]:
            g.append(f"[{i}:a]atrim=start={pre:.3f}:duration={dur:.3f},asetpts=PTS-STARTPTS,volume={vol},"
                     f"aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]")
        else:
            g.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{dur:.3f},asetpts=PTS-STARTPTS[a{i}]")
        vl.append(f"[v{i}]")
        al.append(f"[a{i}]")
    n_in = len(segs)
    if n_in == 1:
        g.append("[v0]null[vcat]")
        g.append("[a0]anull[acat]")
    else:
        g.append("".join(f"[v{i}][a{i}]" for i in range(n_in)) + f"concat=n={n_in}:v=1:a=1[vcat][acat]")
    cur = "vcat"
    # --- overlays de imagen
    for k_i, o in enumerate([o for o in spec["overlays"] if o["type"] == "image"]):
        asset = get_asset(o["asset"])
        if not asset:
            continue
        idx = n_in + len([x for x in inputs if x == "-loop"])
        inputs += ["-loop", "1", "-framerate", "30", "-t", f"{total + 0.5:.2f}", "-i", asset["path"]]
        end = o["end"] if o["end"] is not None else total
        g.append(f"[{idx}:v]scale=w={max(2, round(o['w'] * W) // 2 * 2)}:h=-2,format=rgba,"
                 f"colorchannelmixer=aa={_f(o['opacity'])}[ov{k_i}]")
        g.append(f"[{cur}][ov{k_i}]overlay=x='{round(o['x'] * W)}-w/2':y='{round(o['y'] * H)}-h/2':"
                 f"enable='between(t,{_f(o['start'])},{_f(end)})':eof_action=pass:shortest=0[vo{k_i}]")
        cur = f"vo{k_i}"
    # --- subtítulos + textos (ASS)
    sub = spec["subtitles"]
    words = timeline_words(spec) if sub["enabled"] else []
    styles, events = _ass_text_overlays(spec, W, H, total)
    if words or events:
        fonts = work / "fonts"
        if config.FONTS_DIR.exists() and not fonts.exists():
            shutil.copytree(config.FONTS_DIR, fonts)
        (work / "cap.ass").write_text(
            captions.build_ass(words, W, H, sub["theme"], sub["position"], anim=sub["anim"], size_mult=sub["size"],
                               extra_styles=styles, extra_events=events), encoding="utf-8")
        g.append(f"[{cur}]ass=cap.ass:fontsdir=fonts[vout]")
    else:
        g.append(f"[{cur}]null[vout]")
    # --- audio: música + atenuación automática bajo la voz
    music = spec["music"]
    voice = "acat"
    if music:
        asset = get_asset(music["asset"])
        mdur = total - music["start"]
        if asset and mdur > 0.5:
            midx = n_in + len([x for x in inputs if x == "-loop"])
            pre_in = (["-stream_loop", "-1"] if music["loop"] else []) + (["-ss", f"{music['offset']:.3f}"] if music["offset"] else [])
            inputs += pre_in + ["-i", asset["path"]]
            fi, fo = min(music["fade_in"], mdur / 2), min(music["fade_out"], mdur / 2)
            chain = (f"[{midx}:a]atrim=duration={mdur:.3f},asetpts=PTS-STARTPTS,aresample=48000,"
                     f"aformat=sample_fmts=fltp:channel_layouts=stereo,volume={_f(music['volume'])}")
            if fi > 0:
                chain += f",afade=t=in:st=0:d={fi:.2f}"
            if fo > 0:
                chain += f",afade=t=out:st={max(0, mdur - fo):.2f}:d={fo:.2f}"
            if music["start"] > 0:
                ms = round(music["start"] * 1000)
                chain += f",adelay={ms}|{ms}"
            g.append(chain + "[mus]")
            norm = ":normalize=0" if ffmpeg_has_option("amix", "normalize") else ""
            if music["duck"]:
                g.append("[acat]asplit=2[vo1][vo2]")
                g.append("[mus][vo2]sidechaincompress=threshold=0.03:ratio=10:attack=20:release=400:makeup=1[musd]")
                g.append(f"[vo1][musd]amix=inputs=2:duration=first:dropout_transition=0{norm}[amixed]")
            else:
                g.append(f"[acat][mus]amix=inputs=2:duration=first:dropout_transition=0{norm}[amixed]")
            voice = "amixed"
    if settings.get("loudnorm", True):
        g.append(f"[{voice}]loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[aout]")
    else:
        g.append(f"[{voice}]anull[aout]")
    draft = quality != "final"
    prof = config.QUALITY_PROFILES.get(settings.get("quality_profile") or "alta", config.QUALITY_PROFILES["alta"])
    fps = 30 if draft else prof["fps"]
    crf = "28" if draft else str(prof["crf"])
    out_opts = ["-map", "[vout]", "-map", "[aout]", "-t", f"{total:.3f}", "-c:v", "libx264", "-preset",
                "ultrafast" if draft else prof["preset"], "-crf", crf, "-pix_fmt", "yuv420p", "-r", str(fps),
                "-c:a", "aac", "-b:a", "128k" if draft else "160k", "-movflags", "+faststart"]
    return {"inputs": inputs, "graph": ";\n".join(g), "out_opts": out_opts, "total": total, "size": (W, H)}


def run_graph(plan: dict, work: Path, out_path: Path, progress=None, cancel: threading.Event | None = None):
    work.mkdir(parents=True, exist_ok=True)
    (work / "graph.txt").write_text(plan["graph"], encoding="utf-8")
    last_err = ""
    for opt in ("-filter_complex_script", "-/filter_complex"):      # ffmpeg 6 y anteriores / ffmpeg 7+
        cmd = [config.ffmpeg(), "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats", *plan["inputs"], opt,
               "graph.txt", *plan["out_opts"], str(out_path)]
        p = subprocess.Popen(cmd, cwd=work, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **config.NOWIN)
        errbuf = []
        t = threading.Thread(target=lambda: errbuf.append(p.stderr.read()), daemon=True)
        t.start()
        for raw in iter(p.stdout.readline, b""):
            if cancel is not None and cancel.is_set():
                p.kill()
                raise media.Cancelled()
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("out_time_ms=") and progress:
                try:
                    progress(min(1.0, int(line.split("=")[1]) / 1e6 / max(plan["total"], 0.1)))
                except ValueError:
                    pass
        p.wait()
        t.join(timeout=5)
        last_err = (errbuf[0] if errbuf else b"").decode("utf-8", "replace")
        if p.returncode == 0:
            return
        if "Unrecognized option" in last_err or "Option not found" in last_err:
            continue
        break
    raise media.MediaError(last_err[-700:] or "ffmpeg falló al renderizar.")


# ====================================================================== sincronía de subtítulos (Whisper)
def _norm(w: str) -> str:
    n = unicodedata.normalize("NFKD", w or "")
    return re.sub(r"[^a-z0-9]+", "", "".join(c for c in n if not unicodedata.combining(c)).lower())


def align_words(edited: list[dict], asr: list[dict]) -> list[dict]:
    """Da a las palabras corregidas por el usuario los tiempos reales del audio (alineación de secuencias)."""
    if not edited or not asr:
        return [dict(w) for w in edited]
    a, b = [_norm(w["w"]) for w in edited], [_norm(w["w"]) for w in asr]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    out: list[dict | None] = [None] * len(edited)
    ops = sm.get_opcodes()
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            for k in range(i2 - i1):
                out[i1 + k] = {**edited[i1 + k], "s": asr[j1 + k]["s"], "e": asr[j1 + k]["e"]}
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal" or i2 <= i1:
            continue
        if tag == "replace" and j2 > j1:
            lo, hi = asr[j1]["s"], asr[j2 - 1]["e"]
        else:     # palabras añadidas por el usuario: caben entre las vecinas ya alineadas
            prev_end = next((out[k]["e"] for k in range(i1 - 1, -1, -1) if out[k]), None)
            nxt = next((out[k]["s"] for k in range(i2, len(out)) if out[k]), None)
            lo = prev_end if prev_end is not None else (asr[j1 - 1]["e"] if j1 > 0 else edited[i1]["s"])
            hi = nxt if nxt is not None else (asr[j1]["s"] if j1 < len(asr) else edited[i2 - 1]["e"])
        if hi - lo < 0.06 * (i2 - i1):
            hi = lo + 0.06 * (i2 - i1)
        total_len = sum(len(edited[k]["w"]) + 1 for k in range(i1, i2))
        cur = lo
        for k in range(i1, i2):
            d = (hi - lo) * (len(edited[k]["w"]) + 1) / total_len
            out[k] = {**edited[k], "s": round(cur, 3), "e": round(cur + d, 3)}
            cur += d
    return [w for w in out if w]


def resync_segment(spec: dict, seg_uid: str, settings: dict) -> dict:
    seg = next((s for s in spec["segments"] if s["uid"] == seg_uid), None)
    if not seg:
        raise media.MediaError("Segmento no encontrado.")
    clip = db.q1("SELECT * FROM clips WHERE id=?", (seg["clip_id"],))
    info = clip_source(clip)
    dur = seg_dur(seg)
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "a.wav"
        media.extract_audio(info["path"], wav, max(0.0, seg["src_in"] - info["base"]), dur)
        res = transcribe.engine.transcribe(wav, settings, offset=seg["src_in"], total=dur)
    inside = [w for w in seg["words"] if seg["src_in"] - 0.05 <= (w["s"] + w["e"]) / 2 < seg["src_out"] + 0.05]
    outside = [w for w in seg["words"] if w not in inside]
    aligned = align_words(inside, res["words"])
    seg["words"] = sorted(outside + aligned, key=lambda w: w["s"])
    return seg


# ====================================================================== seguimiento (rostro / objeto)
def analyze_face(spec: dict, seg_uid: str) -> tuple[str, dict]:
    from . import tracking
    seg = next((s for s in spec["segments"] if s["uid"] == seg_uid), None)
    if not seg:
        raise media.MediaError("Segmento no encontrado.")
    clip = db.q1("SELECT * FROM clips WHERE id=?", (seg["clip_id"],))
    px = ensure_proxy(clip)
    r = tracking.track_face(px["path"], 0, px["duration"], fps=5)
    if not r["samples"]:
        raise media.MediaError("No se detectó ningún rostro en este clip. Prueba el seguimiento de objeto.")
    tid = f"face:{seg_uid}"
    return tid, {"kind": "face", "seg": seg_uid, "label": "Rostro", "detected": r["detected"],
                 "samples": [[round(px["base"] + t, 3), x, y] for t, x, y in r["samples"]]}


def analyze_object(spec: dict, seg_uid: str, src_t: float, box: dict, label: str = "Objeto") -> tuple[str, dict]:
    from . import tracking
    seg = next((s for s in spec["segments"] if s["uid"] == seg_uid), None)
    if not seg:
        raise media.MediaError("Segmento no encontrado.")
    clip = db.q1("SELECT * FROM clips WHERE id=?", (seg["clip_id"],))
    px = ensure_proxy(clip)
    b = {k: num(box.get(k), 0.1, 0, 1) for k in ("x", "y", "w", "h")}
    if b["w"] < 0.01 or b["h"] < 0.01:
        raise media.MediaError("Dibuja un recuadro sobre el objeto.")
    r = tracking.track_object(px["path"], 0, px["duration"], clamp(src_t - px["base"], 0, px["duration"]), b)
    tid = "obj" + uuid.uuid4().hex[:5]
    return tid, {"kind": "object", "seg": seg_uid, "label": label[:40], "confidence": r["confidence"],
                 "samples": [[round(px["base"] + t, 3), x, y] for t, x, y in r["samples"]]}


# ====================================================================== proyectos
def _row(r: dict) -> dict:
    spec = json.loads(r["spec"] or "{}")
    return {**{k: r[k] for k in ("id", "name", "status", "progress", "message", "export_clip_id", "created_at", "updated_at")},
            "spec": sanitize(spec) if spec else default_spec()}


def create_project(clip_ids: list[int], name: str = "", user_id: int = 0) -> int:
    clips = []
    for cid in clip_ids[:MAX_SEGMENTS]:
        c = db.q1("SELECT * FROM clips WHERE id=?", (int(cid),))
        if c and c["path"]:
            clips.append(c)
    if not clips:
        raise media.MediaError("Selecciona al menos un clip.")
    if any(c.get("origin") == "edit" for c in clips):
        raise media.MediaError("Un clip que ya salió del editor no se puede usar como material. "
                               "Ábrelo con \"Reabrir en el editor\" o selecciona los clips originales.")
    s = db.get_settings()
    spec = default_spec()
    spec["layout"] = clips[0]["layout"] if clips[0]["layout"] in ("blur", "fill", "original", "gaming") else s["layout"]
    spec["subtitles"]["theme"] = clips[0]["theme"] if clips[0]["theme"] in captions.THEMES else s["caption_theme"]
    spec["subtitles"]["position"] = s["caption_position"]
    spec["segments"] = [segment_from_clip(c) for c in clips]
    title = name.strip() or (clips[0]["title"][:50] if len(clips) == 1 else f"Edición de {len(clips)} clips")
    pid = db.run("INSERT INTO editor_projects(name,spec,status,user_id,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                 (title, json.dumps(spec), "idle", user_id, db.now(), db.now()))
    prepare_async(pid)
    return pid


def get_project(pid: int) -> dict:
    r = db.q1("SELECT * FROM editor_projects WHERE id=?", (pid,))
    if not r:
        raise media.MediaError("Proyecto del editor no encontrado.")
    return _row(r)


def save_project(pid: int, spec: dict, name: str | None = None) -> dict:
    clean = sanitize(spec)
    try:
        fit_to_sources(clean)
    except media.MediaError:
        pass
    db.run("UPDATE editor_projects SET spec=?, name=COALESCE(?,name), updated_at=? WHERE id=?",
           (json.dumps(clean), (name or "").strip()[:80] or None, db.now(), pid))
    return clean


def add_clip(pid: int, clip_id: int) -> dict:
    p = get_project(pid)
    clip = db.q1("SELECT * FROM clips WHERE id=?", (int(clip_id),))
    if not clip or not clip["path"]:
        raise media.MediaError("Clip no encontrado.")
    if clip.get("origin") == "edit":
        raise media.MediaError("Un clip ya editado no se puede añadir como material.")
    if len(p["spec"]["segments"]) >= MAX_SEGMENTS:
        raise media.MediaError("Máximo de clips en un proyecto alcanzado.")
    p["spec"]["segments"].append(segment_from_clip(clip))
    save_project(pid, p["spec"])
    prepare_async(pid)
    return get_project(pid)


def list_projects(user_id: int = 0, is_admin: bool = False) -> list[dict]:
    out = []
    for r in db.q("SELECT * FROM editor_projects ORDER BY updated_at DESC LIMIT 100"):
        if not is_admin and r.get("user_id", 0) not in (0, user_id):
            continue
        spec = sanitize(json.loads(r["spec"] or "{}"))
        first = spec["segments"][0]["clip_id"] if spec["segments"] else 0
        out.append({"id": r["id"], "name": r["name"], "status": r["status"], "updated_at": r["updated_at"],
                    "segments": len(spec["segments"]), "duration": round(total_duration(spec), 1), "cover": first,
                    "export_clip_id": r["export_clip_id"]})
    return out


def delete_project(pid: int):
    db.run("DELETE FROM editor_projects WHERE id=?", (pid,))
    shutil.rmtree(EXPORT_DIR / f"p{pid}", ignore_errors=True)


def proxies_status(spec: dict) -> dict:
    out = {}
    for seg in spec["segments"]:
        cid = seg["clip_id"]
        if cid in out:
            continue
        info = proxy_info(cid)
        out[cid] = ({"ready": True, "base": info["base"], "duration": info["duration"], "w": info["w"], "h": info["h"],
                     "src_w": info.get("src_w", info["w"]), "src_h": info.get("src_h", info["h"]),
                     "fallback": info.get("fallback", False), "ext": Path(info["path"]).suffix.lstrip(".")}
                    if info else {"ready": False})
    return out


_prep_errors: dict[int, str] = {}


def prepare_async(pid: int):
    def work():
        try:
            for seg in get_project(pid)["spec"]["segments"]:
                clip = db.q1("SELECT * FROM clips WHERE id=?", (seg["clip_id"],))
                if clip:
                    ensure_proxy(clip)
            _prep_errors.pop(pid, None)
        except Exception as e:  # noqa: BLE001
            _prep_errors[pid] = str(e)[:300]
    threading.Thread(target=work, daemon=True, name=f"proxy-{pid}").start()


def prep_error(pid: int) -> str:
    return _prep_errors.get(pid, "")


# ---- exportación
EXPORTS: dict[int, threading.Event] = {}


def start_export(pid: int, quality: str = "final") -> None:
    if pid in EXPORTS and not EXPORTS[pid].is_set() and db.q1("SELECT status FROM editor_projects WHERE id=?", (pid,))["status"] == "exporting":
        raise media.MediaError("Este proyecto ya se está exportando.")
    ev = EXPORTS[pid] = threading.Event()
    db.run("UPDATE editor_projects SET status='exporting', progress=0, message='Preparando render…' WHERE id=?", (pid,))
    threading.Thread(target=_export, args=(pid, quality, ev), daemon=True, name=f"export-{pid}").start()


def cancel_export(pid: int):
    if pid in EXPORTS:
        EXPORTS[pid].set()


def draft_path(pid: int) -> Path:
    return EXPORT_DIR / f"p{pid}" / "draft.mp4"


def _export(pid: int, quality: str, cancel: threading.Event):
    try:
        p = get_project(pid)
        spec = fit_to_sources(p["spec"])
        settings = db.get_settings()
        work = EXPORT_DIR / f"p{pid}" / ("draft_work" if quality != "final" else "work")
        plan = build_render(spec, quality, settings, work)
        prog = lambda f: db.run("UPDATE editor_projects SET progress=?, message=? WHERE id=?",  # noqa: E731
                                (f, f"Renderizando… {int(f * 100)}%", pid))
        if quality != "final":
            out = draft_path(pid)
            out.parent.mkdir(parents=True, exist_ok=True)
            run_graph(plan, work, out, prog, cancel)
            db.run("UPDATE editor_projects SET status='draft', progress=1, message='Vista previa exacta lista' WHERE id=?", (pid,))
            return
        first_clip = db.q1("SELECT * FROM clips WHERE id=?", (spec["segments"][0]["clip_id"],))
        score = max((db.q1("SELECT score FROM clips WHERE id=?", (s["clip_id"],)) or {"score": 0})["score"] for s in spec["segments"])
        words = timeline_words(spec) if spec["subtitles"]["enabled"] else []
        text = " ".join(w["w"] for w in words)
        cid = p["export_clip_id"] if p["export_clip_id"] and db.q1("SELECT id FROM clips WHERE id=?", (p["export_clip_id"],)) else 0
        fields = (first_clip["job_id"], p["name"], 0.0, plan["total"], score, "Editado en el editor", json.dumps(words), text,
                  spec["subtitles"]["theme"], spec["layout"])
        if not cid:
            cid = db.run("INSERT INTO clips(job_id,title,start,end,score,reason,words,transcript,theme,layout,path,thumb,"
                         "source_path,origin,editor_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,'','','','edit',?,?)",
                         (*fields, pid, db.now()))
        out = config.JOBS_DIR / str(first_clip["job_id"]) / "clips" / str(cid) / "clip.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        run_graph(plan, work, out, prog, cancel)
        thumb = out.parent / "thumb.jpg"
        try:
            media.thumbnail(out, thumb, 1.0)
        except media.MediaError:
            pass
        db.run("UPDATE clips SET job_id=?,title=?,start=?,end=?,score=?,reason=?,words=?,transcript=?,theme=?,layout=?,"
               "path=?,thumb=?,origin='edit',editor_id=?,source_path='' WHERE id=?",
               (*fields, str(out), str(thumb) if thumb.exists() else "", pid, cid))
        db.run("UPDATE editor_projects SET status='done', progress=1, message='Exportado a la Biblioteca', export_clip_id=? WHERE id=?", (cid, pid))
    except media.Cancelled:
        db.run("UPDATE editor_projects SET status='idle', message='Exportación cancelada' WHERE id=?", (pid,))
    except Exception as e:  # noqa: BLE001
        db.run("UPDATE editor_projects SET status='error', message=? WHERE id=?", (str(e)[:480], pid))
