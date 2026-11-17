"""Procesa un video (URL o archivo): descarga → transcripción por palabra → candidatos → IA → clips."""
import json
import shutil
import threading
import traceback
from pathlib import Path

from . import ai, config, creators, db, highlight, media, tracking, transcribe

CANCEL: dict[int, threading.Event] = {}
RENDER_SLOTS = threading.Semaphore(2)
OVERRIDABLE = ("clip_min_s", "clip_max_s", "max_clips", "min_score", "layout", "caption_theme", "captions",
               "language", "caption_position", "live_segment_s", "live_min_score", "live_max_clips_per_hour",
               "live_wait_minutes", "quality_profile", "cam_corner", "tiktok_mode", "content_type",
               "watermark_mode", "watermark_text", "watermark_opacity")
CONTENT_TYPES = ("general", "entrevista", "podcast", "resumen", "pelicula", "gaming")


def job_settings(job: dict) -> dict:
    s = db.get_settings()
    opts = json.loads(job.get("options") or "{}")
    for k, v in (opts.get("settings") or {}).items():
        if k in OVERRIDABLE and v not in (None, ""):
            default = config.DEFAULT_SETTINGS.get(k)
            s[k] = type(default)(v) if default is not None and not isinstance(default, bool) else bool(v)
    # El perfil de calidad fija fps / CRF / preset del render y, cuando el
    # modelo de transcripción está en "auto", el modelo de Whisper.
    if str(s.get("content_type", "general")).lower() not in CONTENT_TYPES:
        s["content_type"] = "general"
    else:
        s["content_type"] = str(s["content_type"]).lower()
    try:
        s["watermark_opacity"] = max(0, min(100, int(s.get("watermark_opacity", 80) or 0)))
    except (TypeError, ValueError):
        s["watermark_opacity"] = 80
    prof = config.QUALITY_PROFILES.get(s.get("quality_profile") or "alta", config.QUALITY_PROFILES["alta"])
    s["render_fps"], s["render_crf"], s["render_preset"] = prof["fps"], prof["crf"], prof["preset"]
    if s.get("whisper_model", "auto") == "auto":
        s["whisper_model"] = prof["whisper"]
    # Modo TikTok (v3.1): clips cortos que arrancan en el gancho.
    if s.get("tiktok_mode", True):
        s["clip_min_s"], s["clip_max_s"], s["max_clips"] = 20, 40, 8
    return s


def job_dir(job_id: int) -> Path:
    d = config.JOBS_DIR / str(job_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def set_job(job_id: int, **f):
    db.job_update(job_id, **f)


def cancel_event(job_id: int) -> threading.Event:
    return CANCEL.setdefault(job_id, threading.Event())


def produce_clip(job_id: int, inputs: list[Path], concat: bool, src_offset: float, cand: dict, words: list[dict],
                 s: dict, dims: tuple[int, int], timeline_end: float) -> tuple[int, str]:
    """Renderiza un clip. cand: {start,end,title,score,reason} en segundos de la línea de tiempo global.
    Devuelve (clip_id, nota): nota es "" salvo en Dúo sin rostro (fallback a vertical)."""
    start = max(src_offset, cand["start"] - float(s["pad_before"]))
    end = min(timeline_end, cand["end"] + float(s["pad_after"]))
    rel = [{"w": w["w"], "s": round(w["s"] - start, 3), "e": round(w["e"] - start, 3), "p": w.get("p", 1.0)}
           for w in words if w["s"] >= start - 0.05 and w["e"] <= end + 0.05]
    text = " ".join(w["w"] for w in rel)
    clip_id = db.run(
        "INSERT INTO clips(job_id,title,start,end,score,reason,moment,summary,path,thumb,words,transcript,theme,"
        "layout,source_path,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, cand["title"], start, end, cand["score"], cand.get("reason", ""), cand.get("moment", ""),
         cand.get("summary", ""), "", "", json.dumps(rel),
         text, s["caption_theme"], s["layout"],
         json.dumps({"inputs": [str(p) for p in inputs], "concat": concat, "offset": src_offset}), db.now()))
    try:
        cdir = job_dir(job_id) / "clips" / str(clip_id)
        out = cdir / "clip.mp4"
        # Entrevista: caras-slot y segmentos de hablante se calculan una
        # vez aquí y se pasan al render (si falla, render_clip los
        # recalcula; si hay menos de 2 personas, cae al vertical con aviso).
        sp_faces = sp_segs = None
        if s["layout"] == "speaker" and not concat and inputs:
            try:
                tf = tracking.track_faces(inputs[0], start - src_offset, max(1.0, end - start),
                                          fps=5.0, max_faces=4).get("faces")
                sp_segs, sp_faces = tracking.speaker_segments(
                    inputs[0], start - src_offset, max(1.0, end - start), tf, rel)
            except Exception:  # noqa: BLE001
                sp_faces = sp_segs = None
        with RENDER_SLOTS:
            _, _, note = media.render_clip(inputs, start - src_offset, end - src_offset, rel, out, s,
                                           s["caption_theme"], s["layout"], bool(s["captions"]), dims[0], dims[1],
                                           concat=concat, faces=sp_faces, speaker_segments=sp_segs)
        thumb = cdir / "thumb.jpg"
        try:
            media.thumbnail(out, thumb, min(1.5, max(0.2, (end - start) / 3)))
        except media.MediaError:
            pass
        db.run("UPDATE clips SET path=?, thumb=? WHERE id=?", (str(out), str(thumb) if thumb.exists() else "", clip_id))
        return clip_id, note
    except Exception:
        db.run("DELETE FROM clips WHERE id=?", (clip_id,))
        shutil.rmtree(job_dir(job_id) / "clips" / str(clip_id), ignore_errors=True)
        raise


def rerender_clip(clip_id: int, theme: str | None = None, layout: str | None = None, captions_on: bool | None = None,
                  words: list[dict] | None = None):
    c = db.q1("SELECT * FROM clips WHERE id=?", (clip_id,))
    if not c:
        raise media.MediaError("Clip no encontrado.")
    src = json.loads(c["source_path"] or "{}")
    inputs = [Path(p) for p in src.get("inputs", [])]
    if not inputs or not all(p.exists() for p in inputs):
        raise media.MediaError("El video original ya no está en disco (¿borraste el proyecto?).")
    s = db.get_settings()
    theme = theme or c["theme"] or s["caption_theme"]
    layout = layout or c["layout"] or s["layout"]
    # La re-renderización respeta la marca de agua DEL PROYECTO (no solo
    # la global): el proyecto guardó sus opciones al crearse.
    try:
        job = db.q1("SELECT options FROM jobs WHERE id=?", (c["job_id"],))
        proj = (json.loads(job.get("options") or "{}") or {}).get("settings") or {}
        for k in ("watermark_mode", "watermark_text", "watermark_opacity"):
            if proj.get(k) not in (None, ""):
                s[k] = proj[k]
    except Exception:  # noqa: BLE001
        pass
    words = words if words is not None else json.loads(c["words"] or "[]")
    pi = media.probe(inputs[0])
    out = Path(c["path"])
    with RENDER_SLOTS:
        _, _, note = media.render_clip(inputs, c["start"] - src.get("offset", 0), c["end"] - src.get("offset", 0),
                                       words, out, {**s, "caption_position": s["caption_position"]}, theme, layout,
                                       s["captions"] if captions_on is None else captions_on, pi["width"],
                                       pi["height"], concat=bool(src.get("concat")))
    try:
        media.thumbnail(out, Path(c["thumb"] or out.parent / "thumb.jpg"), 1.0)
    except media.MediaError:
        pass
    db.run("UPDATE clips SET theme=?, layout=?, words=?, transcript=? WHERE id=?",
           (theme, layout, json.dumps(words), " ".join(w["w"] for w in words), clip_id))
    return note


def select_clips(cands: list[dict], s: dict) -> list[dict]:
    # candidates() ya devuelve la lista ordenada por diversidad de momentos:
    # no reordenar por score aquí, solo filtrar por min_score en ese orden.
    n, min_score = int(s["max_clips"]), int(s["min_score"])
    good = [c for c in cands if c["score"] >= min_score][:n]
    if len(good) < 3:                       # nunca dejar un video sin resultados útiles
        ranked = sorted(cands, key=lambda c: -c["score"])
        good = ranked[:min(3, n)]
    return good


def _duo_window_ok(path, cand: dict) -> bool:
    """¿La cámara está en TODO el tramo candidato? (puerta del Dúo,
    v3.1.3: en la prueba del usuario con v3.1.2 la cámara desaparecía a
    mitad del clip y el panel superior mostraba gameplay.) Se mide con la
    caja EXACTA por tercios (tracking.detect_cam_box): si la caja está en
    los tres tercios (>= 0.15 de frames por tercio), pasa. Medido en el
    Bugatti: ventanas con cámara todo el rato 0.25-1.00 por tercio; la
    ventana s0600 30-60s —cámara ausente al final, verificado a mano—
    0.10 en el último tercio. NO se usa la presencia del track de la cara:
    Haar pierde la cara pequeña durante tercios enteros aunque la cámara
    esté ahí (s0600 0-30s: presencia del track [0.0, 0.07, 0.67] con la
    cámara visible en los tres tercios). Si no hay cámara de esquina, no
    es ventana de Dúo (False); si hay cara pero no se pudo medir la caja,
    se deja pasar (fail-open) y el render usa la ventana ancha de reserva.
    Si el análisis falla, también se deja pasar."""
    try:
        dur = max(1.0, cand["end"] - cand["start"])
        tf = tracking.track_faces(path, cand["start"], dur, fps=3.0, max_faces=4)
        corner = next((f for f in (tf.get("faces") or []) if f.get("corner")), None)
        if not corner:
            return False
        box = tracking.detect_cam_box(path, cand["start"], dur, corner)
        if not box:
            return True
        return all(t >= 0.15 for t in box.get("thirds", []))
    except Exception:  # noqa: BLE001
        return True


def _gate_duo_picks(job_id: int, path, cands: list[dict], picks: list[dict]) -> list[dict]:
    """Solo para layout Dúo: valida los elegidos en orden y completa con
    los siguientes candidatos hasta tener los mismos que había (o menos,
    si no hay suficientes que pasen). Solo se analizan las ventanas
    elegidas y las necesarias para completar, no todos los candidatos.
    Si ninguna pasa, se devuelven los elegidos originales (el render ya
    cae al vertical o usa la ventana ancha de reserva)."""
    if not picks:
        return picks
    pool = list(picks) + [c for c in cands if c not in picks]
    passing = []
    for c in pool:
        if len(passing) >= len(picks):
            break
        if _duo_window_ok(path, c):
            passing.append(c)
        else:
            db.log(job_id, f"Dúo: descartado el tramo {c['start']:.0f}-{c['end']:.0f}s "
                            "(la cámara no está presente en todo el tramo).")
    return passing or picks


def run_video(job_id: int):
    cancel = cancel_event(job_id)
    job = db.q1("SELECT * FROM jobs WHERE id=?", (job_id,))
    s = job_settings(job)
    jd = job_dir(job_id)
    try:
        # 1) obtener el archivo
        src = job["source"]
        if src.startswith(("http://", "https://")):
            set_job(job_id, status="downloading", message="Descargando video…", progress=0.01)
            db.log(job_id, f"Descargando {src}")
            path, title = media.download_video(
                src, jd, s, lambda p: set_job(job_id, progress=0.01 + p * 0.14,
                                              message=f"Descargando… {int(p * 100)}%"), cancel)
            if title:
                set_job(job_id, title=title[:200])
            try:   # creador real del canal (metadatos del sitio)
                creators.refine_from_info(job_id, json.loads((jd / "source.info.json").read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001
                pass
        else:
            path = Path(src)
            if not path.exists():
                raise media.MediaError("El archivo ya no existe.")
        info = media.probe(path)
        if not info["has_audio"]:
            raise media.MediaError("El video no tiene pista de audio: no hay nada que transcribir.")
        set_job(job_id, duration=info["duration"], progress=0.15)
        # 2) transcripción exacta por palabra
        set_job(job_id, status="transcribing", message="Transcribiendo con Whisper (palabra por palabra)…")
        model, device, _ = transcribe.engine.plan(s) if hasattr(transcribe.engine, "plan") else ("?", "?", "")
        db.log(job_id, f"Whisper: modelo {model} en {device}")
        res = transcribe.transcribe_media(
            path, s, progress=lambda p: set_job(job_id, progress=0.15 + p * 0.45,
                                                message=f"Transcribiendo… {int(p * 100)}%"), cancel=cancel)
        words = res["words"]
        (jd / "words.json").write_text(json.dumps(words), encoding="utf-8")
        set_job(job_id, language=res.get("language", ""))
        if len(words) < 15:
            raise media.MediaError("No se detectó suficiente voz en el video para crear clips.")
        # 3) análisis
        set_job(job_id, status="analyzing", progress=0.62, message="Buscando los mejores momentos…")
        energy = media.audio_energy(path)
        lo, hi, n = float(s["clip_min_s"]), float(s["clip_max_s"]), int(s["max_clips"])
        cands = highlight.candidates(words, energy, lo, hi, max(24, n * 3),
                                     content_type=s.get("content_type", "general"))
        if not cands:
            raise media.MediaError("No se encontraron momentos con la duración pedida. Prueba ampliar la duración.")
        info_ai = {}
        set_job(job_id, message=f"Evaluando {len(cands)} candidatos con IA…")
        cands = ai.rerank(s, cands, info_ai)
        db.log(job_id, f"Motor de IA: {info_ai.get('engine')}")
        picks = select_clips(cands, s)
        if s.get("layout") == "duo":
            set_job(job_id, message="Comprobando que la cámara esté en todo el clip…")
            picks = _gate_duo_picks(job_id, path, cands, picks)
        # 4) render
        set_job(job_id, status="rendering", progress=0.72)
        made = 0
        notes = []
        for i, c in enumerate(picks):
            if cancel.is_set():
                raise media.Cancelled()
            set_job(job_id, message=f"Renderizando clip {i + 1}/{len(picks)}…", progress=0.72 + 0.27 * i / len(picks))
            _, note = produce_clip(job_id, [path], False, 0.0, c, words, s, (info["width"], info["height"]),
                                   info["duration"])
            if note and note not in notes:
                notes.append(note)
                db.log(job_id, note)
            made += 1
        msg = f"{made} clips listos · {info_ai.get('engine', '')}"
        if notes:
            msg += " · " + " ".join(notes)
        set_job(job_id, status="done", progress=1.0, message=msg[:480])
    except media.Cancelled:
        set_job(job_id, status="cancelled", message="Cancelado")
    except media.MediaError as e:
        db.log(job_id, f"ERROR: {e}")
        set_job(job_id, status="error", message=str(e)[:480])
    except Exception as e:  # noqa: BLE001
        db.log(job_id, "ERROR: " + traceback.format_exc()[-400:])
        set_job(job_id, status="error", message=f"{type(e).__name__}: {e}"[:480])


def start_video(job_id: int):
    t = threading.Thread(target=run_video, args=(job_id,), daemon=True, name=f"job-{job_id}")
    t.start()
    return t


# ------------------------------------------------------------------ canal
def channel_children(job_id: int) -> list[int]:
    """IDs de los proyectos-video hijos de un proyecto de canal."""
    job = db.q1("SELECT options FROM jobs WHERE id=?", (job_id,))
    try:
        ids = (json.loads(job.get("options") or "{}") or {}).get("channel_children") or []
    except (json.JSONDecodeError, AttributeError):
        ids = []
    return [int(i) for i in ids if str(i).isdigit()]


def _save_channel_children(job_id: int, ids: list[int]):
    job = db.q1("SELECT options FROM jobs WHERE id=?", (job_id,))
    opts = json.loads(job.get("options") or "{}") if job else {}
    opts["channel_children"] = ids
    db.run("UPDATE jobs SET options=? WHERE id=?", (json.dumps(opts), job_id))


def run_channel(job_id: int):
    """Modo canal: lista los videos más recientes y procesa cada uno con run_video,
    uno a la vez. Un video que falle no detiene los demás."""
    cancel = cancel_event(job_id)
    job = db.q1("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job:
        return
    s = job_settings(job)
    opts = json.loads(job.get("options") or "{}")
    max_videos = max(1, min(20, int(opts.get("max_videos") or 5)))
    children: list[int] = []
    try:
        set_job(job_id, status="downloading", message="Listando videos del canal…", progress=0.01)
        db.log(job_id, f"Listando canal {job['source']}")
        videos = media.list_channel_videos(job["source"], s, max_videos, cancel)
        db.log(job_id, f"Canal: {len(videos)} video(s) — " +
                ", ".join(v["title"][:50] for v in videos[:3]) + ("…" if len(videos) > 3 else ""))
        total_clips, failed = 0, []
        parent_settings = (opts.get("settings") or {})
        creator_given = bool(opts.get("creator_given"))
        for i, v in enumerate(videos):
            if cancel.is_set():
                raise media.Cancelled()
            child_id = db.run(
                "INSERT INTO jobs(kind,title,source,status,options,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                ("video", v["title"][:200], v["url"], "queued",
                 json.dumps({"settings": parent_settings, "creator_given": creator_given,
                             "channel_parent": job_id}),
                 db.now(), db.now()))
            children.append(child_id)
            _save_channel_children(job_id, children)
            creators.detect_for_new_job(child_id, v["url"], job.get("creator") if creator_given else "")
            set_job(job_id, message=f"Video {i + 1}/{len(videos)}: {v['title'][:80]}",
                    progress=(i / len(videos)) * 0.95)
            db.log(job_id, f"Video {i + 1}/{len(videos)}: {v['title']}")
            # si cancelan el canal mientras corre este video, que el video también pare
            _stop_watch = threading.Event()

            def _watch(parent=job_id, child=child_id, stop=_stop_watch):
                while not stop.wait(0.5):
                    if cancel_event(parent).is_set():
                        cancel_event(child).set()
                        return

            threading.Thread(target=_watch, daemon=True, name=f"watch-{child_id}").start()
            try:
                run_video(child_id)                   # síncrono: uno a la vez
            finally:
                _stop_watch.set()
            child = db.q1("SELECT status FROM jobs WHERE id=?", (child_id,)) or {}
            made = db.q1("SELECT COUNT(*) n FROM clips WHERE job_id=?", (child_id,))["n"]
            total_clips += made
            if child.get("status") != "done":
                failed.append(v["title"][:60])
            set_job(job_id, progress=((i + 1) / len(videos)) * 0.95)
        if cancel.is_set():
            raise media.Cancelled()
        if total_clips == 0:
            set_job(job_id, status="error", progress=1.0,
                    message="Ningún video del canal dio clips. Revisa los proyectos hijos para ver el error.")
        else:
            msg = f"{total_clips} clips de {len(videos) - len(failed)}/{len(videos)} videos"
            if failed:
                msg += f" · {len(failed)} video(s) fallaron"
            set_job(job_id, status="done", progress=1.0, message=msg[:480])
            db.log(job_id, msg)
    except media.Cancelled:
        for cid in children:
            cancel_event(cid).set()
        set_job(job_id, status="cancelled", message="Cancelado")
    except media.MediaError as e:
        db.log(job_id, f"ERROR: {e}")
        set_job(job_id, status="error", message=str(e)[:480])
    except Exception as e:  # noqa: BLE001
        db.log(job_id, "ERROR: " + traceback.format_exc()[-400:])
        set_job(job_id, status="error", message=f"{type(e).__name__}: {e}"[:480])


def start_channel(job_id: int):
    t = threading.Thread(target=run_channel, args=(job_id,), daemon=True, name=f"channel-{job_id}")
    t.start()
    return t
