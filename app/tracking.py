"""Seguimiento de rostros y objetos para el reencuadre automático del editor.

Se analiza una copia de baja resolución del video (el "proxy"): rápido y suficiente para ubicar el sujeto.
Devuelve muestras [[t, cx, cy], ...] con coordenadas normalizadas (0-1) del centro del sujeto, ya suavizadas
(sin tirones) para que el reencuadre se vea como el de un operador de cámara.
Requiere opencv-python-headless + numpy (se instalan con iniciar.bat).
"""
import subprocess

from . import config, media

try:
    import cv2
    import numpy as np
except Exception:  # noqa: BLE001
    cv2 = np = None


class TrackingUnavailable(Exception):
    pass


def available() -> bool:
    return cv2 is not None


def _need():
    if cv2 is None:
        raise TrackingUnavailable("Falta OpenCV. Cierra la app y ejecuta iniciar.bat para instalarlo.")


def extract_frames(video, start: float, dur: float, fps: float = 6.0, width: int = 320, color: bool = False,
                   max_h: int | None = None):
    """Frames reducidos: (array [N,H,W] gris o [N,H,W,3] RGB, lista de tiempos relativos a `start`).
    max_h: si el alto del proxy lo supera, se reduce el ancho en proporción (un
    vertical 1080x1920 a 640 de ancho sería 640x1138 y cuadruplica el coste)."""
    _need()
    info = media.probe(video)
    if not info["width"]:
        raise media.MediaError("No se pudo leer el video para analizarlo.")
    h = max(2, round(width * info["height"] / info["width"] / 2) * 2)
    if max_h and h > max_h:
        width = max(2, int(width * max_h / h) // 2 * 2)
        h = max(2, int(max_h) // 2 * 2)
    pix = "rgb24" if color else "gray"
    ch = 3 if color else 1
    cmd = [config.ffmpeg(), "-v", "error", "-ss", f"{max(0.0, start):.3f}", "-t", f"{dur:.3f}", "-i", str(video),
           "-vf", f"fps={fps},scale={width}:{h},format={pix}", "-f", "rawvideo", "-pix_fmt", pix, "-"]
    p = subprocess.run(cmd, capture_output=True, timeout=600, **config.NOWIN)
    if p.returncode != 0:
        raise media.MediaError(p.stderr.decode("utf-8", "replace")[-300:])
    buf = np.frombuffer(p.stdout, dtype=np.uint8)
    n = len(buf) // (width * h * ch)
    if n == 0:
        raise media.MediaError("El tramo no tiene imagen.")
    frames = buf[: n * width * h * ch].reshape((n, h, width, 3) if color else (n, h, width))
    return frames, [i / fps for i in range(n)]


# ------------------------------------------------------------------ suavizado
def _zero_phase(vals: list[float], alpha: float) -> list[float]:
    """Filtro exponencial hacia delante y luego hacia atrás (fase cero: sin retraso), con reflexión en los
    extremos para que el inicio y el final no tengan transitorios."""
    n = len(vals)
    if n < 3:
        return list(vals)
    pad = min(12, n - 1)
    head = [2 * vals[0] - vals[k] for k in range(pad, 0, -1)]
    tail = [2 * vals[-1] - vals[-1 - k] for k in range(1, pad + 1)]
    seq = head + list(vals) + tail

    def ema(v):
        out, cur = [], v[0]
        for x in v:
            cur = alpha * x + (1 - alpha) * cur
            out.append(cur)
        return out
    back = ema(ema(seq)[::-1])[::-1]
    return back[pad:pad + n]


def smooth_path(ts: list[float], xs: list[float], ys: list[float], alpha: float = 0.35, deadband: float = 0.012):
    """Suavizado sin retraso y con zona muerta: la cámara solo se mueve cuando el sujeto se mueve de verdad."""
    sx, sy = _zero_phase(xs, alpha), _zero_phase(ys, alpha)

    def hold(vals):
        out, cur = [], vals[0]
        for v in vals:
            if abs(v - cur) > deadband:
                cur = v
            out.append(cur)
        return out
    # la zona muerta crea escalones: se vuelve a suavizar para que el movimiento sea continuo
    return _zero_phase(hold(sx), 0.5), _zero_phase(hold(sy), 0.5)


def _fill_gaps(vals: list):
    """Rellena None interpolando; los extremos repiten el valor más cercano."""
    idx = [i for i, v in enumerate(vals) if v is not None]
    if not idx:
        return None
    out = list(vals)
    for i in range(len(out)):
        if out[i] is None:
            prev = max((j for j in idx if j < i), default=None)
            nxt = min((j for j in idx if j > i), default=None)
            if prev is None:
                out[i] = vals[nxt]
            elif nxt is None:
                out[i] = vals[prev]
            else:
                f = (i - prev) / (nxt - prev)
                out[i] = vals[prev] + (vals[nxt] - vals[prev]) * f
    return out


# ------------------------------------------------------------------ rostros
# v3.1.2: la cámara del streamer es pequeña (~40 px en 640x360, ~20 px en
# el proxy de 320) y Haar no la veía (2-5% de frames). El proxy de rostros
# sube a 640 de ancho; en vertical el alto se topa en 720 para no
# multiplicar el coste por 4 (640x1138). Medido en el vídeo Bugatti de
# rdjavi: detección de la esquina 3-5% -> 37-42% (mn=5) / ~60% (mn=3).
_FACE_PROXY_WIDTH = 640
_FACE_PROXY_MAX_H = 720
# Preferencia por la cámara de la esquina: un track vale como «cámara» si
# su centro mediano cae en una esquina SUPERIOR, se detectó en >=15% de
# los frames y es espacialmente estable (>=70% de sus centros a <=0.15
# del mediano). Solo esquinas superiores: la cámara del streamer va
# arriba (izquierda o derecha); abajo vive el gameplay — en el clip de
# rdjavi un personaje del juego de espaldas abajo a la izquierda cumplía
# «esquina + persistente + estable» y secuestraba faces[0]. Un personaje
# del juego barre la pantalla (estabilidad baja) y no cumple esto aunque
# pase el umbral 0.05 de media.py; una cámara real no se mueve.
_CORNER = 0.35
_CORNER_MIN_DETECTED = 0.15
_CORNER_MIN_STABLE = 0.6  # v3.1.2: 0.70 -> 0.60, medido (evaluación de puerta):
# la cámara de rdjavi en el Bugatti 5-33s tiene estabilidad 0.630 y con
# 0.70 se rechazaba; con 0.60 pasa. Los falsos positivos de la pelota
# roja están en cy 0.81-0.84 (nunca esquina superior), así que no entran.
_STABLE_RADIUS = 0.15
_cascades = None


def _track_stats(xs: list, ys: list):
    """(centro mediano x, y, estabilidad) de un track a partir de sus
    centros detectados (sin rellenar). Estabilidad = fracción de centros
    a <= _STABLE_RADIUS del mediano; una cámara fija ronda 1.0, un
    personaje del juego que cruza la pantalla queda muy por debajo."""
    pts = [(x, y) for x, y in zip(xs, ys) if x is not None]
    if not pts:
        return None
    sx = sorted(p[0] for p in pts)
    sy = sorted(p[1] for p in pts)
    mx, my = sx[len(sx) // 2], sy[len(sy) // 2]
    near = sum(1 for (x, y) in pts if ((x - mx) ** 2 + (y - my) ** 2) ** 0.5 <= _STABLE_RADIUS)
    return mx, my, near / len(pts)


def _load_cascades():
    global _cascades
    _need()
    if _cascades is None:
        base = cv2.data.haarcascades
        _cascades = [cv2.CascadeClassifier(base + n) for n in ("haarcascade_frontalface_default.xml",
                                                               "haarcascade_profileface.xml")]
        if _cascades[0].empty():
            raise TrackingUnavailable("No se encontró el modelo de rostros de OpenCV.")
    return _cascades


def detect_faces(gray) -> list[tuple[int, int, int, int]]:
    """Rostros (x,y,w,h) en un frame gris. Reemplazable en pruebas.
    Una sola pasada frontal sobre CLAHE (contraste local: la ecualización
    global falla con fondos uniformes). Antes se hacían tres pasadas
    (frontal CLAHE + frontal gris + perfil); el gris casi nunca añade
    nada sobre CLAHE y triplicaba el coste. minNeighbors 5 -> 3: con
    caras de ~40 px el 5 descartaba demasiadas (medido: 21 -> 31
    detecciones de esquina en 50 frames); los falsos positivos extra se
    controlan en track_faces (preferencia por cámara de esquina estable),
    no subiendo el umbral. Si no hay frontal, se prueba el perfil a media
    resolución (barato; un rostro de perfil grande se sigue viendo)."""
    front, profile = _load_cascades()
    h, w = gray.shape
    min_side = max(18, int(min(h, w) * 0.07))
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4)).apply(gray)
    faces = front.detectMultiScale(clahe, scaleFactor=1.1, minNeighbors=3, minSize=(min_side, min_side))
    if len(faces):
        return [tuple(int(v) for v in f) for f in faces]
    if not profile.empty():
        small = cv2.resize(clahe, (max(2, w // 2), max(2, h // 2)))
        faces = profile.detectMultiScale(small, scaleFactor=1.15, minNeighbors=5,
                                         minSize=(max(9, min_side // 2),) * 2)
        return [tuple(2 * int(v) for v in f) for f in faces]
    return []


def track_face(video, start: float, dur: float, fps: float = 5.0) -> dict:
    """Sigue al rostro principal (el más grande y consistente). samples: [[t, cx, cy]] con t relativo a `start`."""
    frames, times = extract_frames(video, start, dur, fps, width=_FACE_PROXY_WIDTH, max_h=_FACE_PROXY_MAX_H)
    H, W = frames[0].shape
    raw_x, raw_y, found = [], [], 0
    last = None
    for fr in frames:
        faces = detect_faces(fr)
        pick = None
        if faces:
            def score(f):
                x, y, w, h = f
                area = (w * h) / (W * H)
                cont = 1.0
                if last:
                    d = ((x + w / 2) / W - last[0]) ** 2 + ((y + h / 2) / H - last[1]) ** 2
                    cont = 1.0 / (1.0 + 12 * d)
                return area * cont
            pick = max(faces, key=score)
        if pick:
            x, y, w, h = pick
            c = ((x + w / 2) / W, (y + h / 2) / H)
            last = c
            found += 1
            raw_x.append(c[0])
            raw_y.append(c[1])
        else:
            raw_x.append(None)
            raw_y.append(None)
    xs, ys = _fill_gaps(raw_x), _fill_gaps(raw_y)
    if xs is None:
        return {"samples": [], "detected": 0.0}
    sx, sy = smooth_path(times, xs, ys)
    return {"samples": [[round(t, 3), round(x, 4), round(y, 4)] for t, x, y in zip(times, sx, sy)],
            "detected": round(found / len(frames), 2)}


def track_faces(video, start: float, dur: float, fps: float = 5.0, max_faces: int = 2) -> dict:
    """Sigue hasta `max_faces` rostros a la vez, con persistencia de identidad.

    Cada frame se asigna cada rostro detectado al track existente con el
    centro más cercano (continuidad); un rostro sin track cercano abre un
    track nuevo. Devuelve {"faces": [{"samples": [[t, cx, cy], ...]
    (suavizadas con smooth_path), "detected": fracción de frames en los que
    se encontró ese rostro, "area": área media del rostro como fracción del
    frame, "corner": True si el track es una cámara de esquina persistente
    y estable}, ...], "count": int}, ordenadas así: primero las cámaras
    de esquina persistentes y estables (por detected, la que más se ve
    primero: es la cámara del streamer y va arriba en el formato Dúo);
    después el resto por visibilidad (detected * area). Si no se detecta
    ningún rostro, faces=[] y count=0. El proxy es de 640 px de ancho
    (antes 320): una cámara pequeña es ~40 px ahí y Haar la ve; a 320
    era ~20 px y se perdía casi siempre.
    """
    _need()
    frames, times = extract_frames(video, start, dur, fps, width=_FACE_PROXY_WIDTH, max_h=_FACE_PROXY_MAX_H)
    H, W = frames[0].shape
    n = len(frames)
    tracks = []  # {xs, ys, found, area_sum, last}
    for i, fr in enumerate(frames):
        dets = []
        for (x, y, w, h) in detect_faces(fr):
            dets.append(((x + w / 2) / W, (y + h / 2) / H, (w * h) / (W * H)))
        # emparejamiento voraz por distancia al último centro conocido
        pairs = []
        for di, d in enumerate(dets):
            for ti, tr in enumerate(tracks):
                if tr["last"] is None:
                    continue
                dist = ((d[0] - tr["last"][0]) ** 2 + (d[1] - tr["last"][1]) ** 2) ** 0.5
                pairs.append((dist, di, ti))
        pairs.sort()
        used_d, used_t, assign = set(), set(), {}
        for dist, di, ti in pairs:
            if dist > 0.35 or di in used_d or ti in used_t:
                continue
            used_d.add(di)
            used_t.add(ti)
            assign[di] = ti
        for ti, tr in enumerate(tracks):
            if ti not in assign.values():
                tr["xs"][i] = None
                tr["ys"][i] = None
        for di, d in enumerate(dets):
            cx, cy, area = d
            if di in assign:
                tr = tracks[assign[di]]
            else:  # rostro nuevo: abre un track
                tr = {"xs": [None] * n, "ys": [None] * n, "found": 0, "area_sum": 0.0, "last": None}
                tracks.append(tr)
            tr["xs"][i], tr["ys"][i] = cx, cy
            tr["found"] += 1
            tr["area_sum"] += area
            tr["last"] = (cx, cy)
    faces = []
    for tr in tracks:
        if not tr["found"]:
            continue
        # presence: detección cruda por muestra, ANTES de rellenar huecos
        # (el relleno inventa posiciones donde no se vio la cara). Sirve
        # para la puerta del Dúo (cámara presente en los tres tercios del
        # clip) y para la atribución de hablante (solo se mide la boca
        # donde la cara se ve de verdad).
        presence = [x is not None for x in tr["xs"]]
        n_third = max(1, len(presence) // 3)
        thirds = []
        for k in range(3):
            part = presence[k * n_third:(k + 1) * n_third if k < 2 else len(presence)]
            thirds.append(round(sum(part) / max(1, len(part)), 2))
        xs, ys = _fill_gaps(tr["xs"]), _fill_gaps(tr["ys"])
        if xs is None:
            continue
        sx, sy = smooth_path(times, xs, ys)
        detected = tr["found"] / n
        area = tr["area_sum"] / tr["found"]
        stats = _track_stats(tr["xs"], tr["ys"])
        corner = False
        if stats is not None:
            mx, my, stab = stats
            in_corner = (mx < _CORNER or mx > 1 - _CORNER) and my < _CORNER
            corner = bool(in_corner and detected >= _CORNER_MIN_DETECTED and stab >= _CORNER_MIN_STABLE)
        faces.append({"samples": [[round(t, 3), round(x, 4), round(y, 4)]
                                  for t, x, y in zip(times, sx, sy)],
                      "detected": round(detected, 2), "area": round(area, 4),
                      "corner": corner, "presence": presence,
                      "presence_thirds": thirds,
                      "_corner": corner, "_vis": detected * area})
    # cámaras de esquina persistentes y estables primero (por detected);
    # el resto por visibilidad, como antes
    faces.sort(key=lambda f: (1 if f["_corner"] else 0, f["detected"] if f["_corner"] else f["_vis"]),
               reverse=True)
    faces = faces[:max(1, max_faces)]
    for f in faces:
        f.pop("_vis", None)
        f.pop("_corner", None)
    return {"faces": faces, "count": len(faces)}


# ------------------------------------------------------------------ caja de la cámara (v3.1.3, punto 4)
def _cam_box_frame(gray, fx: float, fy: float):
    """Caja candidata de la cámara en UN frame, (x_izq, y_sup, x_der, y_inf)
    en px, o None. La cámara del streamer es un rectángulo con borde oscuro
    pegado a una esquina superior: se buscan sus cuatro bordes como líneas
    rectas de Canny. Superior e izquierdo: la línea más fuerte cerca del
    borde del frame. Inferior: la PRIMERA fila fuerte bajo la cara (el
    rótulo "RD JAVI" y el horizonte quedan por debajo y no se alcanzan).
    Derecho: la columna MÁS FUERTE entre la cara y 0.45W dentro de la
    altura de la caja. Por qué no primera-fuerte a la derecha: el borde
    real es tenue en muchos frames (30-74 px de Canny) y el pie de
    micrófono (60-80) lo taparía; por qué no mediana simple de los
    candidatos: el pilar del puente, más alto que la caja, ganaba el
    argmax en algunos frames y la mediana salía de 202 px en vez de 140
    (Bugatti 5-25s). La agregación entre frames (ver detect_cam_box) es
    la que descarta esos fallos: el borde real está siempre en la misma x,
    el pilar se mueve con el juego."""
    H, W = gray.shape
    e = cv2.Canny(gray, 50, 150) > 0
    band = e[:int(0.12 * H), :int(0.40 * W)].sum(axis=1)          # borde superior
    y_top = int(np.argmax(band))
    if band[y_top] < 0.25 * int(0.40 * W):
        return None
    band = e[:int(0.60 * H), :int(0.12 * W)].sum(axis=0)          # borde izquierdo
    x_left = int(np.argmax(band))
    if band[x_left] < 0.25 * int(0.60 * H):
        return None
    # borde inferior: primera fila fuerte bajo la cara
    x1b = min(W, x_left + int(0.30 * W))
    y0, y1 = int((fy + 0.03) * H), int(0.65 * H)
    rows = e[y0:y1, x_left:x1b].sum(axis=1)
    y_bot = None
    for k, c in enumerate(rows):
        if c >= 0.45 * (x1b - x_left):
            y_bot = y0 + k
            break
    if y_bot is None:
        if len(rows) == 0 or rows.max() < 0.25 * (x1b - x_left):
            return None
        y_bot = int(np.argmax(rows)) + y0
    # borde derecho: columna más fuerte a la derecha de la cara, solo
    # dentro de la altura de la caja
    x0, x1 = int((fx + 0.03) * W), int(0.45 * W)
    cols = e[y_top:y_bot + 1, x0:x1].sum(axis=0)
    if len(cols) == 0 or cols.max() < 0.2 * (y_bot - y_top + 1):
        return None
    x_right = int(np.argmax(cols)) + x0
    return (x_left, y_top, x_right, y_bot)


def _hist_peak(vals, width: int = 10, min_frac: float = 0.25, rightmost: bool = True):
    """Pico del histograma de `vals` (cubos de `width` px): el cubo más
    frecuente si rightmost=False, el cubo más a la DERECHA con al menos
    min_frac de los valores si rightmost=True. Devuelve la mediana de los
    valores de ese cubo, o None si ningún cubo llega a min_frac."""
    if not vals:
        return None
    bins = {}
    for v in vals:
        bins[int(v // width)] = bins.get(int(v // width), 0) + 1
    good = [b for b, c in bins.items() if c >= min_frac * len(vals)]
    if not good:
        return None
    b = max(good) if rightmost else max(bins, key=bins.get)
    return float(np.median([v for v in vals if int(v // width) == b]))


def detect_cam_box(video, start: float, dur: float, face: dict | None, fps: float = 2.0):
    """Caja EXACTA de la cámara del streamer (x, y, w, h normalizados) o
    None si no se encuentra con seguridad. Medido en el Bugatti de rdjavi
    (640x360), caja visible ~(0.005, 0.01, 0.22, 0.39), casi cuadrada; el
    detector devuelve (0.005, 0.008-0.019, 0.203-0.234, 0.383-0.394) en las
    seis ventanas medidas (s0600 0-30, 5-35, 5-25, 30-60; s1200 y s2000
    0-30), con 24-48 candidatos de 40-60 frames. Método: por frame se
    sacan los cuatro bordes candidatos; entre frames se agrega por
    histograma —izquierda/arriba por mediana, derecha por el cubo más a
    la derecha con >=25% de los frames (el borde real está siempre en la
    misma x; el pie de micrófono, más a la izquierda, y el pilar del
    puente, que se mueve con el juego, no llegan o no se repiten), abajo
    por el cubo más frecuente—. La mediana simple fallaba: en 5-25s daba
    202 px de ancho (el pilar ganaba) y en 5-35s 83 px (el micrófono).
    El centro de la cara NO se toma del track (su mediana se desvía hasta
    (0.20, 0.34) porque el seguimiento persigue el micrófono entre
    detecciones), sino de la mediana de las detecciones Haar en bruto en
    la esquina (0.08-0.11, 0.16-0.24 en las seis ventanas). La caja es
    estática en un directo, así que una sola caja vale para todo el clip.
    Si la cámara está a la derecha, se espeja el frame y se deshace el
    espejo al devolver la caja. Sin rostro de esquina o sin bordes claros,
    devuelve None y el que llama usa el recorte anterior."""
    _need()
    if not face or not face.get("samples"):
        return None
    mxs = sorted(s[1] for s in face["samples"])
    mys = sorted(s[2] for s in face["samples"])
    track_fx, track_fy = mxs[len(mxs) // 2], mys[len(mys) // 2]
    frames, _ = extract_frames(video, start, dur, fps=fps, width=_FACE_PROXY_WIDTH,
                               max_h=_FACE_PROXY_MAX_H)
    H, W = frames[0].shape
    flip = track_fx > 0.5
    # centro de la cara a partir de las detecciones Haar en bruto en la
    # esquina (los mismos frames que se usan para los bordes): la cara
    # está quieta, así que su mediana es estable aunque el track derive.
    cx, cy = [], []
    for fr in frames:
        f = cv2.flip(fr, 1) if flip else fr
        for (x, y, w, h) in detect_faces(f):
            mx, my = (x + w / 2) / W, (y + h / 2) / H
            if mx < 0.35 and my < 0.45:
                cx.append(mx)
                cy.append(my)
    if len(cx) >= 3:
        fx, fy = float(np.median(cx)), float(np.median(cy))
    else:
        fx, fy = (1 - track_fx) if flip else track_fx, track_fy
    per_frame = []
    for fr in frames:
        f = cv2.flip(fr, 1) if flip else fr
        per_frame.append(_cam_box_frame(f, fx, fy))
    boxes = [b for b in per_frame if b]
    if len(boxes) < max(5, int(0.25 * len(frames))):
        return None
    x_left = float(np.median([b[0] for b in boxes]))
    y_top = float(np.median([b[1] for b in boxes]))
    x_right = _hist_peak([b[2] for b in boxes], rightmost=True)
    y_bot = _hist_peak([b[3] for b in boxes], rightmost=False)
    if x_right is None or y_bot is None:
        return None
    x, y, w, h = x_left, y_top, x_right - x_left, y_bot - y_top
    # presencia de la caja por tercios: fracción de frames de cada tercio
    # cuya caja coincide con la final (sirve para la puerta del Dúo: si
    # la cámara desaparece a mitad del clip, el tercio afectado cae cerca
    # de 0; medido en el Bugatti: ventanas con cámara todo el rato 0.25-
    # 1.00 por tercio, ventana s0600 30-60s —cámara ausente al final,
    # verificado a mano en el frame t=55— 0.10 en el último tercio).
    thirds = []
    n = len(per_frame)
    for k in range(3):
        part = per_frame[k * n // 3:(k + 1) * n // 3 if k < 2 else n]
        ok = sum(1 for b in part
                 if b and abs((b[2] - b[0]) - w) <= 0.25 * w
                 and abs((b[3] - b[1]) - h) <= 0.25 * h)
        thirds.append(round(ok / max(1, len(part)), 2))
    if flip:
        x = W - x - w
    nx, ny, nw, nh = x / W, y / H, w / W, h / H
    if not (0.03 <= nw <= 0.5 and 0.03 <= nh <= 0.65 and 0.35 <= nw / nh <= 2.8):
        return None
    # fx está en coordenadas espejadas si flip: se deshace el espejo
    # antes de comprobar que la cara cae dentro de la caja.
    ofx = 1 - fx if flip else fx
    if not (nx - 0.05 <= ofx <= nx + nw + 0.05 and ny - 0.05 <= fy <= ny + nh + 0.05):
        return None
    return {"x": round(nx, 3), "y": round(ny, 3), "w": round(nw, 3), "h": round(nh, 3),
            "hit": round(len(boxes) / len(frames), 2), "thirds": thirds}


# ------------------------------------------------------------------ hablante (v3.1.3, punto 2)
# Aproximación, NO diarización real: no hay modelo de diarización en CPU
# ni separación de voces. Lo que hace: agrupa las palabras de Whisper en
# enunciados, detecta las caras frame a frame, las agrupa en personas por
# posición+tamaño, y atribuye cada enunciado a la persona cuya boca se
# mueve más (diferencia de frames en la mitad inferior de su cara).
# Límites honestos: si el que habla está fuera de plano, se encuadra a la
# persona visible; si dos caras se parecen en posición y tamaño, el
# agrupamiento puede mezclarlas; con una sola persona no hay cambio de
# cámara (devuelve un solo segmento). Un corte de plano dispara la
# diferencia en TODA la imagen, así que esos pares de frames se excluyen.
def speaker_segments(video, start: float, dur: float, faces: list | None, words: list[dict]):
    """Devuelve (segmentos, caras): segmentos = [(t0, t1, modo)] en
    segundos relativos a `start`, cubriendo [0, dur] sin huecos; modo es
    un índice (plano solo de esa persona) o una tupla (i, j) (pantalla
    dividida: las dos hablan a la vez, v3.1.4). Caras = lista de dicts
    tipo track_faces (una por persona) para el recorte.
    `words`: [{s, e, ...}] con tiempos RELATIVOS al inicio del clip.
    Si hay menos de 2 personas, segmentos=[] y el que llama cae al
    vertical con aviso."""
    _need()
    dur = max(1.0, float(dur))
    words = sorted([w for w in (words or []) if w.get("s") is not None],
                   key=lambda w: w["s"])
    if not words:
        return [], []
    # enunciados: hueco > 0.7 s entre palabras
    utts = [[float(words[0]["s"]), float(words[0]["e"])]]
    for w in words[1:]:
        if float(w["s"]) - utts[-1][1] > 0.7:
            utts.append([float(w["s"]), float(w["e"])])
        else:
            utts[-1][1] = max(utts[-1][1], float(w["e"]))
    frames, times = extract_frames(video, start, dur, fps=5.0, width=_FACE_PROXY_WIDTH,
                                   max_h=_FACE_PROXY_MAX_H)
    H, W = frames[0].shape
    n = len(frames)
    # detecciones por frame: (cx, cy, área, x, y, w, h) en px
    per_frame = []
    for fr in frames:
        dets = []
        for (x, y, w, h) in detect_faces(fr):
            dets.append(((x + w / 2) / W, (y + h / 2) / H, (w * h) / (W * H), x, y, w, h))
        per_frame.append(sorted(dets, key=lambda d: -d[2]))
    # agrupar detecciones en personas (posición + tamaño parecidos).
    # Umbrales ESTRECHOS, medidos en el clip de El Dotol: Andrea
    # (cy~0.36, área~0.06) y Boica (cy~0.26, área~0.09) están a solo 0.10
    # de distancia entre sí; con 0.15 se fundían en una sola persona y no
    # había cambio de cámara. La cara de una misma persona apenas se mueve
    # de frame a frame (la mediana del slot sigue su deriva lenta).
    slots = []  # {cx, cy, area, hits: {frame: det}, }
    for i, dets in enumerate(per_frame):
        used = set()
        for d in dets:
            best, best_dist = None, 0.08
            for si, s in enumerate(slots):
                if si in used:
                    continue
                dist = ((d[0] - s["cx"]) ** 2 + (d[1] - s["cy"]) ** 2) ** 0.5
                ratio = max(d[2], s["area"]) / max(1e-6, min(d[2], s["area"]))
                if dist < best_dist and ratio < 1.6:
                    best, best_dist = si, dist
            if best is None:
                slots.append({"cx": d[0], "cy": d[1], "area": d[2], "hits": {i: d}})
            else:
                s = slots[best]
                used.add(best)
                s["hits"][i] = d
                xs = sorted(h[0] for h in s["hits"].values())
                ys = sorted(h[1] for h in s["hits"].values())
                ar = sorted(h[2] for h in s["hits"].values())
                s["cx"], s["cy"] = xs[len(xs) // 2], ys[len(ys) // 2]
                s["area"] = ar[len(ar) // 2]
    slots = [s for s in slots if len(s["hits"]) >= max(3, int(0.06 * n))]
    slots.sort(key=lambda s: -len(s["hits"]))
    slots = slots[:4]
    slot_faces = []
    for s in slots:
        xs = [None] * n
        ys = [None] * n
        presence = [False] * n
        for i, d in s["hits"].items():
            xs[i], ys[i] = d[0], d[1]
            presence[i] = True
        fx, fy = _fill_gaps(xs), _fill_gaps(ys)
        if fx is None:
            continue
        sx, sy = smooth_path(times, fx, fy)
        slot_faces.append({"samples": [[round(t, 3), round(x, 4), round(y, 4)]
                                       for t, x, y in zip(times, sx, sy)],
                           "detected": round(len(s["hits"]) / n, 2),
                           "area": round(s["area"], 4), "corner": False,
                           "presence": presence})
    if len(slot_faces) < 2:
        # reserva: usar los tracks que pasó el llamador (p. ej. si la
        # detección propia falló pero track_faces sí separó personas)
        fb = []
        for f in (faces or []):
            if f.get("samples") and f.get("detected", 0) >= 0.05:
                fb.append(f)
            if len(fb) >= 4:
                break
        if len(fb) >= 2:
            slot_faces = fb
        else:
            return [], slot_faces
    # energía de boca por persona y por par de frames: diferencia media
    # en la mitad inferior de su cara; se excluyen los pares con cambio
    # global grande (corte de plano: todo cambia a la vez).
    gdiff = [0.0] * n
    for i in range(1, n):
        gdiff[i] = float(np.abs(frames[i].astype("int16") - frames[i - 1].astype("int16")).mean())
    mouth = []  # mouth[slot][i] = energía del par (i-1, i), o -1 si no vale
    for si, s in enumerate(slots if len(slot_faces) == len(slots) else []):
        row = [-1.0] * n
        for i in range(1, n):
            if gdiff[i] > 20 or i not in s["hits"]:
                continue
            d = s["hits"][i]
            _, _, _, x, y, w, h = d
            x0, x1 = max(0, x), min(W, x + w)
            y0, y1 = max(0, y + h // 2), min(H, y + h)
            if x1 - x0 < 4 or y1 - y0 < 4:
                continue
            row[i] = float(np.abs(frames[i][y0:y1, x0:x1].astype("int16")
                                  - frames[i - 1][y0:y1, x0:x1].astype("int16")).mean())
        mouth.append(row)
    if not mouth:  # se usó la reserva de `faces`: sin cajas por frame no
        # se puede medir la boca; se reparte por presencia en cada enunciado
        mouth = None
    # Subdividir los enunciados donde cambia la persona en plano: en una
    # entrevista los hablantes se alternan sin dejar huecos de 0.7 s en el
    # audio (en el clip de El Dotol todo el audio es UN enunciado de 49 s),
    # así que solo con huecos no habría ningún cambio de cámara. Cada
    # persona deja "rachas" de detección (se toleran huecos de 2 frames:
    # Haar pierde la cara suelta); los inicios y fines de racha parten el
    # enunciado. Una racha del micrófono también parte (aparece justo con
    # su dueño) y la atribución por energía de boca decide quién habla.
    change_points: list[float] = []
    if len(slot_faces) == len(slots):
        for si, s in enumerate(slots):
            if s["area"] < 0.04:
                continue
            hit_idx = sorted(s["hits"])
            if not hit_idx:
                continue
            runs = [[hit_idx[0], hit_idx[0]]]
            for i in hit_idx[1:]:
                if i - runs[-1][1] <= 3:  # hueco de hasta 2 frames
                    runs[-1][1] = i
                else:
                    runs.append([i, i])
            for a, b in runs:
                # >= 3 frames a 5 fps (0.6 s); comparar por ÍNDICES, no por
                # segundos: 4.6-4.0 da 0.5999999... en coma flotante y el
                # umbral 0.6 lo rechazaba.
                if b - a >= 3:
                    change_points += [times[a], times[b]]
    change_points = sorted(set(change_points))
    segs = []
    prev = 0
    for (u0, u1) in utts:
        bounds = [u0] + [c for c in change_points if u0 < c < u1] + [u1]
        for a, b in zip(bounds, bounds[1:]):
            if b - a < 0.2:
                continue
            # Atribución por energía de boca, solo entre las caras grandes
            # presentes en el tramo: los falsos positivos pequeños (el
            # micrófono, la planta) también se mueven un poco y robarían el
            # turno; se excluye todo slot con área < 0.5x la mayor presente.
            present = []
            for si in range(len(slot_faces)):
                if mouth is not None:
                    ok = any(mouth[si][i] >= 0 for i in range(1, n)
                             if a <= times[i] <= b)
                else:
                    pr = slot_faces[si].get("presence") or []
                    ok = any(i < len(pr) and pr[i] for i, t in enumerate(times)
                             if a <= t <= b)
                if ok:
                    present.append(si)
            if present:
                amax = max(slot_faces[si].get("area", 0) for si in present)
                present = [si for si in present
                           if slot_faces[si].get("area", 0) >= 0.5 * amax]
            best, best_e = prev, -1.0
            energies = {}
            for si in present:
                if mouth is not None:
                    vals = [mouth[si][i] for i in range(1, n)
                            if times[i] >= a and times[i] <= b and mouth[si][i] >= 0]
                    energies[si] = sum(vals) / len(vals) if vals else -1.0
                else:
                    f = slot_faces[si]
                    pr = f.get("presence") or []
                    energies[si] = (sum(1 for i, t in enumerate(times)
                                        if a <= t <= b and i < len(pr) and pr[i]) / max(1, n))
            for si, e in energies.items():
                if e > best_e:
                    best, best_e = si, e
            # v3.1.4 — pantalla dividida: si DOS personas hablan a la vez
            # (la segunda con >= 0.5x la energía de boca de la primera y
            # ambas >= 1.5, el suelo de ruido), el modo es la tupla (i, j)
            # ordenada; si no, plano solo del primero. Solo con energía
            # de boca real (mouth no es None): con la reserva por
            # presencia las fracciones no son energía y no se aplica.
            # Aproximación honesta (~50%): puede dispararse con risas o
            # con dos bocas moviéndose a la vez sin hablar.
            mode = best
            if mouth is not None and len(energies) >= 2 and best_e >= 1.5:
                ranked = sorted(energies.items(), key=lambda kv: -kv[1])
                (s1, e1), (s2, e2) = ranked[0], ranked[1]
                if e2 >= 1.5 and e2 >= 0.5 * e1:
                    mode = tuple(sorted((s1, s2)))
            segs.append([max(0.0, a), min(dur, b), mode])
            prev = mode
    if not segs:
        return [], slot_faces
    # mínimo 0.8 s por segmento (NO 1.5 s: medido en el clip de El Dotol,
    # los turnos reales de Boica duran 0.6-1.4 s y con 1.5 s se borraban
    # TODOS: el resultado era un solo cambio en 49 s; con 1.0 s el plano
    # inicial de Andrea (0.8 s) se fundía en el de Boica y quedaba mal.
    # Con 0.8 s sobreviven los turnos de ~1 s; los más cortos se funden
    # con el vecino del MISMO hablante si lo hay, si no con el más largo).
    _MIN_SEG = 0.8
    changed = True
    while changed and len(segs) > 1:
        changed = False
        for k, (a, b, sp) in enumerate(segs):
            if b - a < _MIN_SEG:
                if k > 0 and segs[k - 1][2] == sp:
                    segs[k - 1][1] = b
                elif k < len(segs) - 1 and segs[k + 1][2] == sp:
                    segs[k + 1][0] = a
                elif k > 0 and (k == len(segs) - 1 or (segs[k - 1][1] - segs[k - 1][0]) >= (segs[k + 1][1] - segs[k + 1][0])):
                    segs[k - 1][1] = b
                elif k < len(segs) - 1:
                    segs[k + 1][0] = a
                else:
                    segs[k - 1][1] = b
                del segs[k]
                changed = True
                break
    # cubrir [0, dur] sin huecos y fundir vecinos con el mismo hablante
    segs[0][0] = 0.0
    segs[-1][1] = dur
    for k in range(1, len(segs)):
        segs[k - 1][1] = segs[k][0]
    merged = [segs[0]]
    for s in segs[1:]:
        if s[2] == merged[-1][2]:
            merged[-1][1] = s[1]
        else:
            merged.append(s)
    return [(round(a, 2), round(b, 2), i) for a, b, i in merged], slot_faces


# ------------------------------------------------------------------ objetos
def track_object(video, start: float, dur: float, t0: float, box: dict, fps: float = 8.0) -> dict:
    """Sigue un objeto marcado por el usuario. box={x,y,w,h} normalizado (0-1) en el instante t0 (relativo a start)."""
    frames, times = extract_frames(video, start, dur, fps, color=True)   # en color: un objeto rojo sobre césped
    H, W = frames[0].shape[:2]                                          # casi no cambia el brillo, pero sí el color
    k0 = min(range(len(times)), key=lambda i: abs(times[i] - t0))
    bw, bh = max(8, int(box["w"] * W)), max(8, int(box["h"] * H))
    bx = int(min(max(0, box["x"] * W), W - bw))
    by = int(min(max(0, box["y"] * H), H - bh))
    base = cv2.GaussianBlur(frames[k0], (3, 3), 0)[by:by + bh, bx:bx + bw].astype("float32")
    n = len(frames)
    pos = [None] * n
    conf = [0.0] * n
    pos[k0], conf[k0] = (bx + bw / 2, by + bh / 2), 1.0

    def run(order):
        tmpl = base.copy()
        cx, cy = bx + bw / 2, by + bh / 2
        for k in order:
            g = cv2.GaussianBlur(frames[k], (3, 3), 0)
            mx, my = int(max(bw, bh) * 1.1) + 6, int(max(bw, bh) * 1.1) + 6
            x0, y0 = int(max(0, cx - bw / 2 - mx)), int(max(0, cy - bh / 2 - my))
            x1, y1 = int(min(W, cx + bw / 2 + mx)), int(min(H, cy + bh / 2 + my))
            region = g[y0:y1, x0:x1].astype("float32")
            if region.shape[0] < bh or region.shape[1] < bw:
                pos[k] = (cx, cy)
                continue
            res = cv2.matchTemplate(region, tmpl, cv2.TM_CCOEFF_NORMED)
            _, mv, _, ml = cv2.minMaxLoc(res)
            if mv >= 0.4:                                   # coincidencia fiable: sigue y adapta la plantilla
                cx, cy = x0 + ml[0] + bw / 2, y0 + ml[1] + bh / 2
                patch = g[y0 + ml[1]:y0 + ml[1] + bh, x0 + ml[0]:x0 + ml[0] + bw].astype("float32")
                if patch.shape == tmpl.shape:
                    tmpl = 0.92 * tmpl + 0.08 * patch
            pos[k], conf[k] = (cx, cy), float(mv)           # si se pierde, conserva la última posición
    run(range(k0 + 1, n))
    run(range(k0 - 1, -1, -1))
    xs = [p[0] / W for p in pos]
    ys = [p[1] / H for p in pos]
    sx, sy = smooth_path(times, xs, ys, alpha=0.5, deadband=0.006)
    return {"samples": [[round(t, 3), round(x, 4), round(y, 4)] for t, x, y in zip(times, sx, sy)],
            "confidence": round(sum(conf) / n, 2)}
