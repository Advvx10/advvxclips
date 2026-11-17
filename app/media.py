"""ffmpeg / ffprobe / yt-dlp."""
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlparse

from . import captions, config


class MediaError(Exception):
    pass


class Cancelled(Exception):
    pass


def _dec(b: bytes) -> str:
    return b.decode("utf-8", "replace")


def run(cmd: list[str], cwd=None, timeout=None) -> str:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout, **config.NOWIN)
    except FileNotFoundError as e:
        raise MediaError(f"No se encontró '{Path(cmd[0]).name}'. Ejecuta iniciar.bat para instalarlo.") from e
    except subprocess.TimeoutExpired as e:
        raise MediaError("Tiempo de espera agotado.") from e
    if p.returncode != 0:
        raise MediaError(_dec(p.stderr)[-800:] or f"{Path(cmd[0]).name} falló ({p.returncode})")
    return _dec(p.stdout)


def probe(path) -> dict:
    out = run([config.ffprobe(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    d = json.loads(out or "{}")
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), None)
    dur = float(d.get("format", {}).get("duration") or (v or {}).get("duration") or 0)
    return {"duration": dur, "width": int((v or {}).get("width") or 0), "height": int((v or {}).get("height") or 0),
            "has_audio": a is not None, "has_video": v is not None}


def extract_audio(src, dst, start: float | None = None, dur: float | None = None):
    cmd = [config.ffmpeg(), "-y", "-loglevel", "error"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(src)]
    if dur:
        cmd += ["-t", f"{dur:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)]
    run(cmd)


def audio_energy(src) -> list[float]:
    """RMS (dB) por segundo. Sirve para detectar gritos, risas y picos de emoción."""
    cmd = [config.ffmpeg(), "-nostats", "-loglevel", "error", "-i", str(src), "-vn", "-ac", "1", "-af",
           "aresample=16000,asetnsamples=n=16000:p=0,astats=metadata=1:reset=1,"
           "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-", "-f", "null", "-"]
    try:
        out = run(cmd, timeout=3600)
    except MediaError:
        return []
    vals = []
    for m in re.finditer(r"RMS_level=(-?[\d.]+|-inf|inf|nan)", out):
        try:
            v = float(m.group(1))
        except ValueError:
            v = -60.0
        vals.append(-60.0 if v != v or v < -60 else min(v, 0.0))
    return vals


def thumbnail(src, dst, at: float = 1.0):
    run([config.ffmpeg(), "-y", "-loglevel", "error", "-ss", f"{at:.2f}", "-i", str(src), "-frames:v", "1",
         "-vf", "scale=360:-2", str(dst)])


# ------------------------------------------------------------------ render
def _cam_crop(corner: str) -> str:
    """Recorte de la cámara dentro del video original (la cámara viene
    incrustada en el directo). La cámara ocupa ~32% del ancho del video."""
    x = {"tl": "0", "bl": "0", "tr": "iw-iw*0.32", "br": "iw-iw*0.32"}[corner]
    y = {"tl": "0", "tr": "0", "bl": "ih-iw*0.32*9/16", "br": "ih-iw*0.32*9/16"}[corner]
    return f"crop=w='iw*0.32':h='iw*0.32*9/16':x='{x}':y='{y}'"


# ------------------------------------------------- expresión lineal por tramos
# Copia local del patrón de editor.pwl (NO importar app.editor: importa
# app.media y sería circular). Reduce los puntos con RDP para que la
# expresión de ffmpeg quede corta.
def _f(x: float) -> str:
    return f"{x:.5f}".rstrip("0").rstrip(".") if "." in f"{x:.5f}" else f"{x:.5f}"


def _rdp(points: list[tuple[float, float]], tol: float) -> list[tuple[float, float]]:
    if len(points) < 3:
        return list(points)
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


def _pwl(points: list[tuple[float, float]]) -> str:
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


def _face_crop(face: dict | None, cw: int, ch: int) -> str:
    """Recorte móvil centrado en el rostro seguido (samples [[t, cx, cy]]
    normalizados, ya suavizados). La ventana queda siempre dentro del frame.
    Si no hay rostro, recorte centrado estático."""
    if face and face.get("samples"):
        pts_x = _rdp([(t, cx) for t, cx, _ in face["samples"]], 0.003)
        pts_y = _rdp([(t, cy) for t, _, cy in face["samples"]], 0.003)
        px, py = _pwl(pts_x), _pwl(pts_y)
        x = f"min(max(0,{px}*iw-{cw // 2}),max(0,iw-{cw}))"
        y = f"min(max(0,{py}*ih-{ch // 2}),max(0,ih-{ch}))"
    else:
        x, y = "(iw-ow)/2", "(ih-oh)/2"
    return f"crop=w={cw}:h={ch}:x='{x}':y='{y}'"


def _median_face_xy(f: dict) -> tuple:
    """Centro mediano (cx, cy) de un track de rostro, o (0.5, 0.5)."""
    s = f.get("samples") or []
    if not s:
        return (0.5, 0.5)
    xs = sorted(p[1] for p in s)
    ys = sorted(p[2] for p in s)
    n = len(s)
    return (xs[n // 2], ys[n // 2])


def _valid_duo_faces(faces: list | None) -> list:
    """Rostros del Dúo que cumplen la puerta, ordenados por visibilidad
    (track_faces ya los ordena: cámaras de esquina primero), como mucho 4.
    Puerta SOLO por esquina (v3.1.2, medido): se acepta un track solo si
    es una cámara de esquina persistente y estable (el flag `corner` de
    track_faces: esquina superior + detected >= 0.15 + estabilidad >= 0.60).
    NO hay rama incondicional detected >= 0.5: la evaluación de puerta
    (18 ventanas + ventanas frescas de la pelota) midió que un 0.5 acepta
    el falso positivo del círculo "1" de la pelota roja en ventanas de
    largo real (det 0.61 en 13.9-41.9s, 0.76 en 20-40s), mientras que
    solo-esquina acepta la cámara de rdjavi en 9/9 ventanas del Bugatti
    y a los dos hombres del video de reacción jobs/1 (5-33s), y rechaza
    todos los falsos positivos de la pelota (cy 0.81-0.84, nunca esquina
    superior). Sin rostro válido, el Dúo cae al vertical (ver render_clip).
    Límite honesto: un rostro real que NO esté en una esquina superior
    (p. ej. centrado en el frame) también cae al vertical con aviso."""
    out = []
    for f in (faces or []):
        if f.get("samples") and f.get("corner"):
            out.append(f)
        if len(out) >= 4:
            break
    # Deduplicar: Haar a veces detecta DOS veces la misma cara (en el
    # Bugatti 5-25s salen dos tracks de esquina, centros a 0.17, uno de
    # ellos un sub-elemento de la cámara —área 0.0049 frente a 0.0126—)
    # y el Dúo lo tomaba por 2 personas y partía la pantalla en dos
    # bandas con la misma cara. Se descarta el track pequeño cuando su
    # centro cae a <0.25 de otro track ya aceptado y su área es <0.5x
    # la de ese track. Dos personas reales no se tocan: sus áreas son
    # comparables (p. ej. Andrea 0.063 frente a Boica 0.09), y dos
    # hombres lado a lado tienen los centros mucho más separados.
    kept = []
    for f in out:
        fx, fy = _median_face_xy(f)
        dup = False
        for g in kept:
            gx, gy = _median_face_xy(g)
            if (fx - gx) ** 2 + (fy - gy) ** 2 < 0.25 ** 2 and f.get("area", 0) < 0.5 * g.get("area", 0):
                dup = True
                break
        if not dup:
            kept.append(f)
    return kept


def _valid_camara_faces(faces: list | None) -> list:
    """Rostros que valen para el formato Cámara completa: visibles
    (detected >= 0.15) y en la mitad superior del frame (cy mediana < 0.6).
    La posición importa: en el video de la pelota roja el falso positivo
    (el círculo con el número, det hasta 0.76 en ventanas largas) vive
    abajo (cy 0.81-0.84) y con esto cae; una cara real hablando a cámara
    está centrada o arriba. Sin rostro válido, Cámara cae al vertical."""
    out = []
    for f in (faces or []):
        if not f.get("samples") or f.get("detected", 0) < 0.15:
            continue
        cys = sorted(s[2] for s in f["samples"])
        if cys[len(cys) // 2] < 0.6:
            out.append(f)
    out.sort(key=lambda f: f.get("detected", 0) * f.get("area", 0), reverse=True)
    return out[:1]


def _duo_window(src_w: int, src_h: int, face: dict | None = None,
                pane_w: int = 1080, pane_h: int = 960, factor: float = 3.0,
                floor: float = 0.4) -> tuple[int, int]:
    """Ventana de recorte para un panel del Dúo/Reacción. El aspecto de la
    ventana es el aspecto del panel (pane_w:pane_h) para no estirar la cara
    al escalar. El tamaño se ajusta al rostro detectado (área media): la
    ventana mide ~`factor`x el ancho del rostro, entre `floor` y el 85%
    del máximo que cabe en el frame. Sin rostro, el 85% del máximo."""
    max_fit = min(src_w or 1080, int((src_h or 1920) * pane_w / pane_h))
    cw = int(max_fit * 0.85)
    if face and face.get("area"):
        fw = (face["area"] * (src_w or 1080) * (src_h or 1920)) ** 0.5
        cw = int(min(max(fw * factor, max_fit * floor), max_fit * 0.85))
    cw = cw // 2 * 2
    ch = int(cw * pane_h / pane_w) // 2 * 2
    return max(2, cw), max(2, ch)


# ------------------------------------------------- expresión escalonada
def _step(bounds: list[tuple[float, float, float]]) -> str:
    """Expresión de ffmpeg (variable t) constante por tramos: bounds son
    (t0, t1, valor) cubriendo [0, dur] sin huecos. Para el formato
    Entrevista: el encuadre SALTA de un hablante a otro en el límite del
    segmento, no se desliza (eso es lo que hace _pwl)."""
    if len(bounds) == 1:
        return _f(bounds[0][2])
    parts = [f"lt(t,{_f(bounds[0][1])})*{_f(bounds[0][2])}"]
    for a, b, v in bounds[1:-1]:
        parts.append(f"gte(t,{_f(a)})*lt(t,{_f(b)})*{_f(v)}")
    parts.append(f"gte(t,{_f(bounds[-1][0])})*{_f(bounds[-1][2])}")
    return "(" + "+".join(parts) + ")"


def _step_crop(segments: list, faces: list, cw: int, ch: int) -> str:
    """Recorte que salta entre hablantes: en cada segmento el centro es la
    mediana del rostro activo en ese tramo (de sus samples). La ventana
    queda siempre dentro del frame, como _face_crop."""
    if not segments or not faces:
        return f"crop=w={cw}:h={ch}:x='(iw-ow)/2':y='(ih-oh)/2'"
    xs, ys = [], []
    for a, b, idx in segments:
        f = faces[idx] if 0 <= idx < len(faces) else faces[0]
        pts = [(s[1], s[2]) for s in f.get("samples", []) if a - 0.01 <= s[0] <= b + 0.01]
        if not pts:
            pts = [(s[1], s[2]) for s in f.get("samples", [])]
        if not pts:
            pts = [(0.5, 0.5)]
        mx = sorted(p[0] for p in pts)[len(pts) // 2]
        my = sorted(p[1] for p in pts)[len(pts) // 2]
        xs.append((a, b, mx))
        ys.append((a, b, my))
    px, py = _step(xs), _step(ys)
    x = f"min(max(0,{px}*iw-{cw // 2}),max(0,iw-{cw}))"
    y = f"min(max(0,{py}*ih-{ch // 2}),max(0,ih-{ch}))"
    return f"crop=w={cw}:h={ch}:x='{x}':y='{y}'"


def _enable_expr(intervals: list) -> str:
    """Expresión de ffmpeg (variable t) que vale 1 dentro de los intervalos
    (t0, t1) y 0 fuera. Para encender un overlay solo en sus tramos. Todos
    los intervalos van acotados por arriba (gte*lt): el último también,
    porque si no quedaría encendido hasta el final del clip."""
    if not intervals:
        return "0"
    parts = [f"gte(t,{_f(a)})*lt(t,{_f(b)})" for a, b in intervals]
    return "(" + "+".join(parts) + ")"


def _wm_xy(position: str, overlay: bool) -> tuple[str, str]:
    """Posición de la marca de agua. overlay=True: variables de overlay
    (W,H,w,h); False (drawtext): variables de drawtext (w,h,tw,th)."""
    if overlay:
        return {"tl": ("24", "24"), "tr": ("W-w-24", "24"),
                "bl": ("24", "H-h-24"), "br": ("W-w-24", "H-h-24")}.get(position, ("W-w-24", "H-h-24"))
    return {"tl": ("24", "24"), "tr": ("w-tw-24", "24"),
            "bl": ("24", "h-th-24"), "br": ("w-tw-24", "h-th-24")}.get(position, ("w-tw-24", "h-th-24"))


def _filter_graph(layout: str, src_w: int, src_h: int, ass: str | None, cam_corner: str = "tl",
                  faces: list | None = None, watermark: dict | None = None,
                  video_idx: int = 0, logo_idx: int | None = None,
                  speaker_segments: list | None = None) -> tuple[str, int, int]:
    """faces: lista de track_faces() (la [0] es la más visible). watermark:
    {"mode": "logo"|"text", "text": str, "position": tl|tr|bl|br, "size": int}
    (logo: input `logo_idx`, se usa tal cual, sin quitarle el fondo).
    video_idx: índice del input de video (1 cuando el logo va primero).
    speaker_segments: [(t0, t1, índice)] de tracking.speaker_segments
    (solo layout "speaker"); las caras son las de `faces` (slots)."""
    V = f"[{video_idx}:v]"
    faces = faces or []
    top_face = faces[0] if faces and faces[0].get("samples") and faces[0].get("detected", 0) >= 0.05 else None
    second_face = faces[1] if len(faces) > 1 and faces[1].get("samples") and faces[1].get("detected", 0) >= 0.05 else None
    if layout == "original":
        w = min(src_w or 1920, 1920) // 2 * 2
        h = (round(w * (src_h or 1080) / (src_w or 1920))) // 2 * 2
        g = f"{V}scale={w}:{h},setsar=1[v0]"
    elif layout == "fill":
        w, h = 1080, 1920
        g = f"{V}crop='min(iw,ih*9/16)':ih,scale={w}:{h},setsar=1[v0]"
    elif layout == "duo":
        # Dúo/Reacción 9:16, generalizado por número de rostros válidos
        # (puerta SOLO por esquina, ver _valid_duo_faces; los más visibles
        # primero, máx. 4):
        #   0 rostros -> NO se parte en dos (eso duplicaba el mismo video
        #     arriba y abajo): se usa el recorte vertical (fill) y
        #     render_clip devuelve la advertencia.
        #   1 rostro  -> Reacción: cámara ANCHA arriba (1080x640, todo el
        #     cuadro de la cámara: cuerpo, micro y cuarto) y el video
        #     completo abajo (1080x1280). v3.1.3: ya no es el recorte
        #     prieto que seguía la cara.
        #   2 rostros -> mitad y mitad (1080x960 cada uno, una cara por banda).
        #   3 rostros -> tres bandas de 1080x640. 4 rostros -> cuatro de 1080x480.
        w, h = 1080, 1920
        valid = _valid_duo_faces(faces)
        if not valid:
            g = f"{V}crop='min(iw,ih*9/16)':ih,scale={w}:{h},setsar=1[v0]"
        elif len(valid) == 1:
            # v3.1.3: cámara ANCHA arriba (1080x640): todo el cuadro de la
            # cámara —cuerpo, micrófono y cuarto— a lo ancho, NO el recorte
            # prieto que seguía la cara (v3.1.2). Si se detectó la caja
            # exacta (tracking.detect_cam_box), se recorta ESA caja y se
            # escala llenando el panel (se recorta arriba/abajo, sin
            # estirar y sin que entre gameplay por los lados); si no, una
            # ventana más ancha alrededor de la cara (~5.5x su ancho).
            box = valid[0].get("cam_box")
            if box:
                bw = max(2, int(box["w"] * (src_w or 1080)) // 2 * 2)
                bh = max(2, int(box["h"] * (src_h or 1920)) // 2 * 2)
                bx = min(max(0, int(box["x"] * (src_w or 1080))), max(0, (src_w or 1080) - bw))
                by = min(max(0, int(box["y"] * (src_h or 1920))), max(0, (src_h or 1920) - bh))
                top = (f"[top]crop={bw}:{bh}:{bx}:{by},"
                       f"scale=1080:640:force_original_aspect_ratio=increase,"
                       f"crop=1080:640,setsar=1[topv]")
            else:
                cw, ch = _duo_window(src_w, src_h, valid[0], 1080, 640, factor=5.5, floor=0.25)
                top = f"[top]{_face_crop(valid[0], cw, ch)},scale=1080:640,setsar=1[topv]"
            bot = (f"[bot]crop='min(iw,ih*1080/1280)':'min(ih,iw*1280/1080)',"
                   f"scale=1080:1280,setsar=1[botv]")
            g = f"{V}split=2[top][bot];{top};{bot};[topv][botv]vstack,setsar=1[v0]"
        else:
            n = len(valid)
            pane_h = 1920 // n
            srcs = "".join(f"[p{i}]" for i in range(n))
            chains, outs = [], []
            for i, face in enumerate(valid):
                cw, ch = _duo_window(src_w, src_h, face, 1080, pane_h)
                chains.append(f"[p{i}]{_face_crop(face, cw, ch)},"
                              f"scale=1080:{pane_h},setsar=1[p{i}v]")
                outs.append(f"[p{i}v]")
            g = f"{V}split={n}{srcs};" + ";".join(chains) + ";" + "".join(outs) + f"vstack=inputs={n},setsar=1[v0]"
    elif layout == "gaming":
        # 9:16 gaming: el gameplay llena el vertical y la cámara del
        # jugador va en una caja arriba. v3.1.3: si se detectó la caja
        # EXACTA de la cámara (tracking.detect_cam_box), se recorta ESA
        # caja: nada de gameplay dentro (antes la esquina fija mostraba
        # árboles/coches cuando la cámara se movía, y el recorte que
        # seguía la cara también se salía de la caja). La caja medida en
        # el Bugatti es casi cuadrada (~140x149 px en 640x360), así que
        # se encaja dentro de 480x270 SIN estirar (forzar 480x270
        # estiraría la cara casi 2x en horizontal); queda ~254x270.
        # Sin caja exacta: recorte que sigue la cara (det>=0.5) o, si no,
        # la esquina fija de siempre (cam_corner). La etiqueta "RD JAVI"
        # es del video original y va dentro de su caja: no se añade otra.
        w, h = 1080, 1920
        corner = cam_corner if cam_corner in ("tl", "tr", "bl", "br") else "tl"
        corner_face = next((f for f in faces if f.get("corner")), None)
        box = corner_face.get("cam_box") if corner_face else None
        game_face = top_face if top_face and top_face.get("detected", 0) >= 0.5 else None
        if box:
            bw = max(2, int(box["w"] * (src_w or 1080)) // 2 * 2)
            bh = max(2, int(box["h"] * (src_h or 1920)) // 2 * 2)
            bx = min(max(0, int(box["x"] * (src_w or 1080))), max(0, (src_w or 1080) - bw))
            by = min(max(0, int(box["y"] * (src_h or 1920))), max(0, (src_h or 1920) - bh))
            cam = f"crop={bw}:{bh}:{bx}:{by}"
            cam_scale = "scale=480:270:force_original_aspect_ratio=decrease"
        elif game_face:
            cw = int((src_w or 1080) * 0.32) // 2 * 2
            ch = int(cw * 9 / 16) // 2 * 2
            cam = _face_crop(game_face, cw, ch)
            cam_scale = "scale=320:180"
        else:
            cam = _cam_crop(corner)
            cam_scale = "scale=320:180"
        g = (f"{V}split=2[main][camsrc];"
             f"[main]crop='min(iw,ih*9/16)':ih,scale={w}:{h},setsar=1[base];"
             f"[camsrc]{cam},{cam_scale},setsar=1,"
             f"drawbox=x=0:y=0:w=iw:h=ih:color=#2f7bff:t=5[cam];"
             f"[base][cam]overlay=24:24,setsar=1[v0]")
    elif layout == "speaker":
        # Entrevista: plano completo 1080x1920 centrado en el hablante
        # activo; el encuadre SALTA de una persona a otra en los límites
        # de speaker_segments (expresión escalonada, no deslizante).
        # v3.1.4 — pantalla dividida: cuando DOS hablan a la vez (modo
        # tupla (i, j) en speaker_segments), se ven AMBOS en dos bandas
        # de 1080x960 (uno arriba, uno abajo); cuando habla uno solo,
        # su plano a pantalla completa; al terminar el cruce se vuelve
        # al plano solo. Sin 2+ personas o sin segmentos, vertical
        # (fill) y render_clip avisa (mismo patrón que el Dúo sin rostro).
        w, h = 1080, 1920
        if speaker_segments and len(faces) >= 2:
            big = max(faces, key=lambda f: f.get("area", 0))
            cw, ch = _duo_window(src_w, src_h, big, 1080, 1920)
            split = [(a, b, m) for a, b, m in speaker_segments if isinstance(m, tuple)]
            if not split:
                g = f"{V}{_step_crop(speaker_segments, faces, cw, ch)},scale={w}:{h},setsar=1[v0]"
            else:
                # plano solo: en los tramos divididos se rellena con el
                # hablante del solo más cercano (no se ve: el overlay
                # solo está apagado en esos tramos)
                solo_bounds = []
                last_solo = 0
                for a, b, m in speaker_segments:
                    if isinstance(m, int):
                        last_solo = m
                        solo_bounds.append((a, b, m))
                    else:
                        solo_bounds.append((a, b, last_solo))
                # bandas: en los tramos solo se rellenan con la pareja
                # del dividido más cercano (tampoco se ven: el overlay
                # dividido está apagado en esos tramos)
                def _band_bounds(which):
                    out = []
                    last_pair = split[0][2]
                    for a, b, m in speaker_segments:
                        if isinstance(m, tuple):
                            last_pair = m
                            out.append((a, b, m[which]))
                        else:
                            out.append((a, b, last_pair[which]))
                    return out
                bcw, bch = _duo_window(src_w, src_h, big, 1080, 960)
                solo_crop = _step_crop(solo_bounds, faces, cw, ch)
                b0_crop = _step_crop(_band_bounds(0), faces, bcw, bch)
                b1_crop = _step_crop(_band_bounds(1), faces, bcw, bch)
                solo_en = _enable_expr([(a, b) for a, b, m in speaker_segments if isinstance(m, int)])
                split_en = _enable_expr([(a, b) for a, b, m in split])
                g = (f"{V}split=4[solo][b0][b1][base];"
                     f"[solo]{solo_crop},scale=1080:1920,setsar=1[soloV];"
                     f"[b0]{b0_crop},scale=1080:960,setsar=1[b0v];"
                     f"[b1]{b1_crop},scale=1080:960,setsar=1[b1v];"
                     f"[b0v][b1v]vstack,setsar=1[splitV];"
                     f"[base]scale=1080:1920,setsar=1[baseV];"
                     f"[baseV][soloV]overlay=enable='{solo_en}'[o1];"
                     f"[o1][splitV]overlay=enable='{split_en}'[v0]")
        else:
            g = f"{V}crop='min(iw,ih*9/16)':ih,scale={w}:{h},setsar=1[v0]"
    elif layout == "camara":
        # Cámara completa: la cámara de la persona llena TODO el vertical
        # 9:16 (cabeza y hombros centrados, cara seguida), sin juego ni
        # segundo video abajo. Ventana 9:16 de ~3.5x el ancho de la cara.
        # Sin rostro válido, vertical (fill) y render_clip avisa.
        w, h = 1080, 1920
        cam_faces = _valid_camara_faces(faces)
        if cam_faces:
            cw, ch = _duo_window(src_w, src_h, cam_faces[0], 1080, 1920, factor=3.5)
            g = f"{V}{_face_crop(cam_faces[0], cw, ch)},scale={w}:{h},setsar=1[v0]"
        else:
            g = f"{V}crop='min(iw,ih*9/16)':ih,scale={w}:{h},setsar=1[v0]"
    else:  # blur: vídeo completo centrado sobre su propio fondo desenfocado
        w, h = 1080, 1920
        # el fondo se desenfoca a 1/4 de resolución y se reescala: mucho más rápido y más suave
        g = (f"{V}split=2[a][b];[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
             f"boxblur=10:2,scale={w}:{h},eq=brightness=-0.08[bg];"
             f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
             f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v0]")
    # subtítulos y marca de agua van sobre el canvas completo, en ese orden
    cur = "[v0]"
    if ass:
        g += f";{cur}ass={ass}:fontsdir=fonts[v1]"
        cur = "[v1]"
    if watermark:
        mode = watermark.get("mode", "logo")
        pos = watermark.get("position", "br") if watermark.get("position") in ("tl", "tr", "bl", "br") else "br"
        try:
            opacity = max(0.0, min(1.0, float(watermark.get("opacity", 0.8))))
        except (TypeError, ValueError):
            opacity = 0.8
        if mode == "logo" and logo_idx is not None:
            size = max(40, int(watermark.get("size", 220) or 220))
            x, y = _wm_xy(pos, overlay=True)
            g += (f";[{logo_idx}:v]scale={size}:-2,format=rgba,"
                  f"colorchannelmixer=aa={_f(opacity)},setsar=1[wm];"
                  f"{cur}[wm]overlay={x}:{y},setsar=1[v]")
        elif mode == "text":
            text = str(watermark.get("text") or "ClipsViralesRD_").replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
            x, y = _wm_xy(pos, overlay=False)
            g += (f";{cur}drawtext=fontfile='fonts/Poppins-Bold.ttf':text='{text}':"
                  f"fontsize=48:fontcolor=white:borderw=2:bordercolor=black:alpha={_f(opacity)}:"
                  f"x={x}:y={y}[v]")
        else:  # logo pedido pero sin input de logo: sin marca de agua
            g += f";{cur}null[v]"
    else:
        g += f";{cur}null[v]"
    return g, w, h


def out_size(layout: str, src_w: int, src_h: int, cam_corner: str = "tl") -> tuple[int, int]:
    return _filter_graph(layout, src_w, src_h, None, cam_corner)[1:]


def _watermark_settings(s: dict) -> tuple[dict | None, Path | None]:
    """Marca de agua según ajustes (los defaults los declara config.py).
    Logo: se usa el archivo tal cual, con su fondo azul a negro, sin
    quitarle el fondo. Si el logo no existe, cae a texto. Opacidad
    0.0-1.0 (config: watermark_opacity 0-100, por defecto 80). Si hay una
    imagen personalizada subida (watermark_image + DATA_DIR/watermark.png
    existe), esa es el logo; si no, el logo por defecto."""
    mode = str(s.get("watermark_mode", "logo") or "logo").lower()
    if mode not in ("logo", "text"):
        return None, None
    pos = str(s.get("watermark_position", "br") or "br")
    if pos not in ("tl", "tr", "bl", "br"):
        pos = "br"
    try:
        opacity = max(0.0, min(1.0, float(s.get("watermark_opacity", 80) or 0) / 100.0))
    except (TypeError, ValueError):
        opacity = 0.8
    logo = config.ROOT / "assets" / "clipsviralesrd-logo.png"
    if s.get("watermark_image"):
        custom = config.DATA_DIR / "watermark.png"
        if custom.exists():
            logo = custom
        elif (config.DATA_DIR / str(s["watermark_image"])).exists():
            logo = config.DATA_DIR / str(s["watermark_image"])
    if mode == "logo" and logo.exists():
        return {"mode": "logo", "position": pos, "opacity": opacity,
                "size": int(s.get("watermark_size", 220) or 220)}, logo
    return {"mode": "text", "position": pos, "opacity": opacity,
            "text": str(s.get("watermark_text", "ClipsViralesRD_") or "ClipsViralesRD_")}, None


def render_clip(inputs: list[Path], start: float, end: float, words_rel: list[dict], out_path: Path, s: dict,
                theme: str, layout: str, with_captions: bool, src_w: int, src_h: int, concat: bool = False,
                faces: list | None = None, speaker_segments: list | None = None):
    """Corta [start,end] (segundos del/los input) y genera el MP4 final con subtítulos.
    inputs: un archivo, o varios .ts consecutivos (live) cuando concat=True; start es relativo a su concatenación.
    Devuelve (ancho, alto, nota): nota es "" salvo en Dúo/Cámara/Entrevista sin rostro, donde avisa del fallback a vertical.
    faces / speaker_segments: solo los pasa pipeline para layout "speaker" (caras-slot ya calculadas); si no se
    pasan, se calculan aquí (mismo resultado, más lento)."""
    out_path = Path(out_path)
    work = out_path.parent
    work.mkdir(parents=True, exist_ok=True)
    fonts = work / "fonts"
    if config.FONTS_DIR.exists() and not fonts.exists():
        shutil.copytree(config.FONTS_DIR, fonts)
    ass_name = None
    gw, gh = out_size(layout, src_w, src_h)
    if with_captions and words_rel:
        ass_name = out_path.stem + ".ass"
        (work / ass_name).write_text(captions.build_ass(words_rel, gw, gh, theme, s.get("caption_position", "low")),
                                     encoding="utf-8")
    # Seguimiento de rostros solo para duo/gaming/camara/speaker y solo con
    # un archivo: con concat (live, varios .ts) no hay un único video al
    # que seguir, así que se usan los fallbacks (esquina fija / recorte
    # centrado). El seguimiento nunca rompe el render: si falla, se sigue
    # sin él. Import local: app.tracking importa app.media (circular si va
    # arriba). Para speaker, pipeline ya pasa las caras-slot y los
    # segmentos; si no vienen, se calculan aquí.
    if faces is None and layout in ("duo", "gaming", "camara", "speaker") and not concat and inputs:
        try:
            from . import tracking
            faces = tracking.track_faces(inputs[0], start, max(1.0, end - start),
                                         fps=5.0, max_faces=4).get("faces") or None
        except Exception:  # noqa: BLE001
            faces = None
    if layout in ("duo", "gaming") and faces:
        # caja exacta de la cámara para el Dúo ancho y el Gaming (v3.1.3)
        try:
            from . import tracking
            corner = next((f for f in faces if f.get("corner")), None)
            if corner:
                corner["cam_box"] = tracking.detect_cam_box(
                    inputs[0], start, max(1.0, end - start), corner)
        except Exception:  # noqa: BLE001
            pass
    if layout == "speaker" and not concat and inputs:
        try:
            from . import tracking
            if speaker_segments is None:
                speaker_segments, slot_faces = tracking.speaker_segments(
                    inputs[0], start, max(1.0, end - start), faces, words_rel)
                if slot_faces:
                    faces = slot_faces
        except Exception:  # noqa: BLE001
            speaker_segments = None
    # Fallbacks con aviso (la nota la sube pipeline hasta el mensaje del
    # trabajo): Dúo sin rostro no se duplica; Cámara sin rostro y
    # Entrevista con menos de 2 personas usan el vertical (fill).
    note = ""
    if layout == "duo" and not _valid_duo_faces(faces):
        note = ("⚠ Sin rostro detectado en este clip: se usó el formato vertical "
                "en vez del Dúo para no duplicar el video.")
    elif layout == "camara" and not _valid_camara_faces(faces):
        note = ("⚠ Sin rostro detectado en este clip: se usó el formato vertical "
                "en vez de Cámara completa.")
    elif layout == "speaker" and (not speaker_segments or len(faces or []) < 2):
        note = ("⚠ No se detectaron 2 personas hablando en este clip: se usó el formato "
                "vertical en vez de Entrevista.")
    wm, logo_path = _watermark_settings(s)
    # El logo entra como primer input (índice 0) para no mover el -ss del
    # concat; el video pasa a ser el input 1 y el audio se mapea desde él.
    video_idx, logo_idx = (1, 0) if (wm and wm["mode"] == "logo" and logo_path is not None) else (0, None)
    graph, _, _ = _filter_graph(layout, src_w, src_h, ass_name, str(s.get("cam_corner", "tl")),
                                faces=faces, watermark=wm, video_idx=video_idx, logo_idx=logo_idx,
                                speaker_segments=speaker_segments)
    dur = max(1.0, end - start)
    cmd = [config.ffmpeg(), "-y", "-loglevel", "error"]
    if logo_path is not None and wm and wm["mode"] == "logo":
        cmd += ["-i", str(logo_path)]
    if concat:
        lst = work / (out_path.stem + ".txt")
        lst.write_text("".join(f"file '{Path(p).as_posix()}'\n" for p in inputs), encoding="utf-8")
        cmd += ["-f", "concat", "-safe", "0", "-i", lst.name, "-ss", f"{start:.3f}"]
    else:
        cmd += ["-ss", f"{start:.3f}", "-i", str(inputs[0])]
    cmd += ["-t", f"{dur:.3f}", "-filter_complex", graph, "-map", "[v]", "-map", f"{video_idx}:a?"]
    af = "aresample=48000"
    if s.get("loudnorm", True):
        af = "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000"
    cmd += ["-af", af, "-c:v", "libx264", "-preset", str(s.get("render_preset", "veryfast")),
            "-crf", str(s.get("render_crf", 20)), "-pix_fmt", "yuv420p", "-r", str(s.get("render_fps", 30)),
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out_path)]
    try:
        run(cmd, cwd=work, timeout=1800)
    finally:
        if concat:
            (work / (out_path.stem + ".txt")).unlink(missing_ok=True)
    return gw, gh, note


# ------------------------------------------------------------------ yt-dlp
_help_cache: dict[str, str] = {}


def ytdlp_supports(flag: str) -> bool:
    exe = config.ytdlp()
    if exe not in _help_cache:
        try:
            _help_cache[exe] = _dec(subprocess.run([exe, "--help"], capture_output=True, timeout=30,
                                                   **config.NOWIN).stdout)
        except Exception:  # noqa: BLE001
            _help_cache[exe] = ""
    return flag in _help_cache[exe]


def ytdlp_base(s: dict, no_playlist: bool = True) -> list[str]:
    a = [config.ytdlp()]
    if no_playlist:
        a += ["--no-playlist"]
    a += ["--no-warnings", "--ignore-config", "--retries", "5"]
    ff = config.ffmpeg()
    if os.path.isabs(ff):
        a += ["--ffmpeg-location", ff]
    if s.get("cookies_file") and Path(s["cookies_file"]).exists():
        a += ["--cookies", s["cookies_file"]]
    elif s.get("cookies_browser"):
        a += ["--cookies-from-browser", s["cookies_browser"]]
    # YouTube exige un runtime JS externo; si no hay deno pero sí node, úsalo
    if ytdlp_supports("--js-runtimes") and not shutil.which("deno") and shutil.which("node"):
        a += ["--js-runtimes", "node"]
    return a


def host_of(url: str) -> str:
    return (urlparse(url if "://" in url else "https://" + url).hostname or "").lower()


def is_direct_media(url: str) -> bool:
    p = urlparse(url).path.lower()
    return p.endswith((".m3u8", ".mp4", ".mkv", ".webm", ".ts", ".mov", ".mpd"))


def explain_ytdlp_error(err: str) -> str:
    e = err.lower()
    if "sign in to confirm" in e or "not a bot" in e:
        return ("YouTube pide verificar que no eres un bot. En Ajustes > Descarga elige tu navegador en "
                "'Cookies del navegador' (con sesión de YouTube iniciada) y reintenta.")
    if "could not copy chrome cookie" in e or "cookie database" in e:
        return "No se pudieron leer las cookies de Chrome/Edge (están bloqueadas). Cierra el navegador o usa Firefox."
    if "403" in e or "cloudflare" in e or "forbidden" in e:
        return ("El sitio bloqueó la descarga (403/Cloudflare). Prueba: Ajustes > Descarga > Cookies del navegador "
                "(Firefox funciona mejor) y actualiza yt-dlp (se actualiza al ejecutar iniciar.bat).")
    if "private" in e or "members-only" in e or "join this channel" in e:
        return "El video es privado o solo para miembros."
    if "unsupported url" in e:
        return "URL no soportada. Pega el enlace directo del video o del directo."
    if "is offline" in e or "not currently live" in e or "this live event will begin" in e or "no live" in e:
        return "El canal no está en directo ahora mismo."
    if "unable to download webpage" in e or "getaddrinfo" in e or "timed out" in e:
        return "Sin conexión a internet o el sitio no responde."
    if "requested format is not available" in e:
        return "Formato no disponible; actualiza yt-dlp (iniciar.bat) o prueba con cookies del navegador."
    return err.strip().splitlines()[-1][:300] if err.strip() else "Error desconocido de yt-dlp."


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def download_video(url: str, dest: Path, s: dict, on_progress=None, cancel: threading.Event | None = None):
    """Descarga un video con yt-dlp. Devuelve (ruta, título)."""
    dest.mkdir(parents=True, exist_ok=True)
    for f in dest.glob("source.*"):
        f.unlink(missing_ok=True)
    variants = [[]]
    if "kick.com" in host_of(url) and ytdlp_supports("--impersonate"):
        variants = [["--impersonate", "chrome"], []]
    h = int(s.get("max_height", 1080))
    last_err = ""
    for extra in variants:
        cmd = ytdlp_base(s) + extra + [
            "-f", "bv*+ba/b", "-S", f"res:{h},vcodec:h264,acodec:aac", "--merge-output-format", "mp4",
            "--newline", "--write-info-json", "--concurrent-fragments", "4", "--fragment-retries", "10",
            "-o", "source.%(ext)s", url]
        p = subprocess.Popen(cmd, cwd=dest, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **config.NOWIN)
        tail = []
        for raw in iter(p.stdout.readline, b""):
            if cancel is not None and cancel.is_set():
                p.kill()
                raise Cancelled()
            line = _ANSI.sub("", _dec(raw)).strip()
            tail.append(line)
            tail[:] = tail[-25:]
            m = re.search(r"\[download\]\s+([\d.]+)%", line)
            if m and on_progress:
                on_progress(float(m.group(1)) / 100)
        p.wait()
        if p.returncode == 0:
            vids = [f for f in dest.glob("source.*") if f.suffix not in (".json", ".part", ".ytdl")]
            if not vids:
                raise MediaError("La descarga terminó pero no se encontró el archivo.")
            title = ""
            info = dest / "source.info.json"
            if info.exists():
                try:
                    title = json.loads(info.read_text(encoding="utf-8")).get("title", "")
                except Exception:  # noqa: BLE001
                    pass
            return max(vids, key=lambda f: f.stat().st_size), title
        last_err = "\n".join(tail)
        if extra and ("impersonate" in last_err.lower() or "curl_cffi" in last_err.lower()):
            continue
        break
    raise MediaError(explain_ytdlp_error(last_err))


# ------------------------------------------------------------------ canal
def normalize_channel_url(url: str) -> str:
    """Un canal de YouTube se lista desde su pestaña /videos (si no se dio una pestaña)."""
    if "youtube.com" not in host_of(url) and "youtu.be" not in host_of(url):
        return url
    parts = [p for p in urlparse(url).path.split("/") if p]
    tabs = {"videos", "shorts", "live", "streams", "playlists", "community", "about", "featured"}
    if parts and parts[-1] in tabs:
        return url
    if parts and (parts[0].startswith("@") or parts[0] in ("channel", "c", "user")):
        return url.rstrip("/") + "/videos"
    return url


def list_channel_videos(url: str, s: dict, max_videos: int = 5,
                        cancel: threading.Event | None = None) -> list[dict]:
    """Lista los videos más recientes de un canal con yt-dlp (sin descargar nada).

    Devuelve [{id, title, url, duration, view_count, upload_date}] en el orden
    del canal (más reciente primero). Solo lista: no descarga ni transcribe.
    """
    if cancel is not None and cancel.is_set():
        raise Cancelled()
    n = max(1, min(20, int(max_videos or 5)))
    url = normalize_channel_url(url)
    cmd = ytdlp_base(s, no_playlist=False) + [
        "--flat-playlist", "--skip-download", "--dump-single-json",
        "--playlist-end", str(n), url]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=300, **config.NOWIN)
    except FileNotFoundError as e:
        raise MediaError(f"No se encontró '{Path(config.ytdlp()).name}'. Ejecuta iniciar.bat para instalarlo.") from e
    except subprocess.TimeoutExpired as e:
        raise MediaError("Listar el canal tardó demasiado. Reintenta.") from e
    if cancel is not None and cancel.is_set():
        raise Cancelled()
    out = _dec(p.stdout or b"")
    if p.returncode != 0:
        raise MediaError(explain_ytdlp_error(out + "\n" + _dec(p.stderr or b"")))
    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        raise MediaError("No se pudo leer la lista del canal (respuesta inesperada de yt-dlp).") from e
    entries = data.get("entries") or []
    if not entries:
        if data.get("_type") == "video" or "/watch" in url:
            raise MediaError("Esa URL es de un video, no de un canal. Usa el modo «Video por URL».")
        raise MediaError("No se encontraron videos en ese canal. Revisa que el enlace sea del canal "
                         "(p. ej. youtube.com/@canal) y que sus videos sean públicos.")
    vids, seen = [], set()
    for e in entries:
        if not isinstance(e, dict):
            continue
        vid = str(e.get("id") or "").strip()
        if not vid or vid in seen:
            continue
        if e.get("_type") not in (None, "video", "url", "url_transparent"):
            continue
        raw = (e.get("webpage_url") or e.get("url") or "").strip()
        if raw.startswith("http"):
            vurl = raw
        elif raw and "youtube.com" in host_of(url):
            vurl = f"https://www.youtube.com/watch?v={vid}"
        else:
            vurl = raw or vid
        seen.add(vid)
        vids.append({"id": vid, "title": (e.get("title") or vid).strip()[:200],
                     "url": vurl, "duration": e.get("duration"),
                     "view_count": e.get("view_count"), "upload_date": e.get("upload_date")})
        if len(vids) >= n:
            break
    if not vids:
        raise MediaError("No se encontraron videos en ese canal.")
    return vids
