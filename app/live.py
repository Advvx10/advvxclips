"""Clips automáticos de directos (YouTube, Kick, Twitch, .m3u8…).

yt-dlp (o ffmpeg directo) → ffmpeg corta la señal en segmentos .ts → cada segmento cerrado se transcribe
(palabra por palabra) y se analiza; cuando aparece un momento fuerte y completo, se renderiza el clip
mientras el directo sigue. Al detener, se procesa lo que quede.
"""
import subprocess
import threading
import time
import traceback
from pathlib import Path

from . import ai, config, creators, db, highlight, media, pipeline, transcribe

SESSIONS: dict[int, "LiveSession"] = {}


def fmt(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02}:{sec % 60:02}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02}"


class LiveSession:
    def __init__(self, job_id: int):
        self.job_id = job_id
        self.job = db.q1("SELECT * FROM jobs WHERE id=?", (job_id,))
        self.s = pipeline.job_settings(self.job)
        self.url = self.job["source"]
        self.dir = pipeline.job_dir(job_id) / "live"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.stop_ev = pipeline.cancel_event(job_id)
        self.segments: list[dict] = []      # {path,start,end} en línea de tiempo global
        self.words: list[dict] = []
        self.energy: list[float] = []
        self.clips: list[tuple[float, float]] = []
        self.clip_times: list[float] = []
        self.cursor = 0.0
        self.timeline_end = 0.0
        self.offset = 0.0
        self.procs: list[subprocess.Popen] = []
        self.dims = (1280, 720)
        self.transcribed_to = 0.0

    # ------------------------------------------------------------ utilidades
    def note(self, text: str):
        db.log(self.job_id, text)

    def status(self, status: str, msg: str):
        db.job_update(self.job_id, status=status, message=msg[:480], duration=self.timeline_end)

    def summary(self) -> str:
        return (f"Grabando {fmt(self.timeline_end)} · transcrito hasta {fmt(self.transcribed_to)} · "
                f"{len(self.clips)} clips")

    # ------------------------------------------------------------ grabación
    def _spawn(self, idx: int, extra: list[str]) -> tuple[subprocess.Popen, subprocess.Popen | None, Path]:
        seg = ["-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-f", "segment",
               "-segment_time", str(int(self.s["live_segment_s"])), "-segment_format", "mpegts",
               "-segment_list", f"run{idx}.csv", "-segment_list_type", "csv", f"r{idx}_%04d.ts"]
        log = self.dir / f"ytdlp{idx}.log"
        if media.is_direct_media(self.url):
            pre = ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10"] \
                if self.url.startswith("http") else []
            cmd = [config.ffmpeg(), "-hide_banner", "-loglevel", "warning", "-y", *pre, "-i", self.url, *seg]
            ff = subprocess.Popen(cmd, cwd=self.dir, stdin=subprocess.DEVNULL, stderr=open(log, "wb"), **config.NOWIN)
            return ff, None, log
        ydl = media.ytdlp_base(self.s) + extra + ["-f", "b[height<=720]/b", "--hls-use-mpegts", "--quiet",
                                                   "-o", "-", self.url]
        p1 = subprocess.Popen(ydl, cwd=self.dir, stdout=subprocess.PIPE, stderr=open(log, "wb"), **config.NOWIN)
        cmd = [config.ffmpeg(), "-hide_banner", "-loglevel", "warning", "-y", "-i", "pipe:0", *seg]
        p2 = subprocess.Popen(cmd, cwd=self.dir, stdin=p1.stdout, stderr=open(self.dir / f"ffmpeg{idx}.log", "wb"),
                              **config.NOWIN)
        p1.stdout.close()
        return p2, p1, log

    def start_recorder(self, idx: int) -> tuple[bool, str]:
        """Devuelve (ok, error_explicado). ok=True si sigue vivo tras unos segundos."""
        variants = [[]]
        if "kick.com" in media.host_of(self.url) and media.ytdlp_supports("--impersonate"):
            variants = [["--impersonate", "chrome"], []]
        err = ""
        for extra in variants:
            ff, y, log = self._spawn(idx, extra)
            self.procs = [p for p in (y, ff) if p]
            t0 = time.time()
            while time.time() - t0 < 9 and not self.stop_ev.is_set():
                if any(p.poll() is not None for p in self.procs):
                    break
                time.sleep(0.4)
            if all(p.poll() is None for p in self.procs):
                return True, ""
            self.kill()
            err = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
            if "impersonate" in err.lower() or "curl_cffi" in err.lower():
                continue
            break
        return False, media.explain_ytdlp_error(err) if err.strip() else "No se pudo iniciar la grabación."

    def kill(self):
        for p in self.procs:
            if p.poll() is None:
                try:
                    p.terminate()
                except Exception:  # noqa: BLE001
                    pass
        for p in self.procs:
            try:
                p.wait(timeout=8)
            except Exception:  # noqa: BLE001
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass

    def alive(self) -> bool:
        return bool(self.procs) and all(p.poll() is None for p in self.procs)

    # ------------------------------------------------------------ segmentos
    def _read_csv(self, idx: int, seen: set) -> list[tuple[str, float, float]]:
        f = self.dir / f"run{idx}.csv"
        rows = []
        if f.exists():
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = line.strip().split(",")
                if len(parts) >= 3 and parts[0] not in seen:
                    try:
                        rows.append((parts[0], float(parts[1]), float(parts[2])))
                    except ValueError:
                        pass
        return rows

    def ingest(self, name: str, t0: float, t1: float):
        path = self.dir / name
        if not path.exists() or t1 - t0 < 1.0:
            return
        g0, g1 = self.offset + t0, self.offset + t1
        if g1 <= self.timeline_end - 0.01:
            return
        if len(self.segments) == 0:
            try:
                pi = media.probe(path)
                if pi["width"]:
                    self.dims = (pi["width"], pi["height"])
            except media.MediaError:
                pass
        self.segments.append({"path": path, "start": g0, "end": g1})
        self.timeline_end = g1
        self.status("recording", self.summary())
        try:
            en = media.audio_energy(path)
            need = int(g0) + len(en)
            self.energy += [-60.0] * (need - len(self.energy))
            for k, v in enumerate(en):
                self.energy[int(g0) + k] = v
            res = transcribe.transcribe_media(path, self.s, offset=g0)
            self.words += res["words"]
            self.transcribed_to = g1
            self.note(f"Segmento {fmt(g0)}–{fmt(g1)}: {len(res['words'])} palabras")
        except Exception as e:  # noqa: BLE001
            self.note(f"No se pudo transcribir {name}: {e}")
            return
        self.status("recording", self.summary())
        self.detect(final=False)

    # ------------------------------------------------------------ detección
    def _overlaps(self, a: float, b: float) -> bool:
        return any(not (b <= s or a >= e) for s, e in self.clips)

    def detect(self, final: bool):
        lo, hi = float(self.s["clip_min_s"]), float(self.s["clip_max_s"])
        limit = self.transcribed_to if final else self.transcribed_to - 6
        region = [w for w in self.words if w["s"] >= self.cursor]
        if len(region) < 15 or limit - self.cursor < lo:
            return
        cands = highlight.candidates(region, self.energy, lo, hi, 12,
                                     content_type=self.s.get("content_type", "general"))
        cands = [c for c in cands if c["end"] <= limit and c["start"] >= self.cursor - 0.01
                 and not self._overlaps(c["start"] - 1, c["end"] + 1)]
        pool = [c for c in cands if c["score"] >= int(self.s["live_min_score"]) - 12][:6]
        if pool:
            info = {}
            pool = ai.rerank(self.s, pool, info)
            self.note(f"Evaluados {len(pool)} candidatos · {info.get('engine')}")
        minimum = int(self.s["live_min_score"])
        now = time.time()
        self.clip_times = [t for t in self.clip_times if now - t < 3600]
        accepted_end = self.cursor
        for c in sorted(pool, key=lambda c: -c["score"]):
            if c["score"] < minimum:
                continue
            if len(self.clip_times) >= int(self.s["live_max_clips_per_hour"]):
                self.note("Límite de clips por hora alcanzado.")
                break
            if self._overlaps(c["start"] - 1, c["end"] + 1):
                continue
            try:
                self._make_clip(c)
                self.clips.append((c["start"], c["end"]))
                self.clip_times.append(time.time())
                accepted_end = max(accepted_end, c["end"])
                self.note(f"Clip creado [{c['score']}] {c['title']}")
            except Exception as e:  # noqa: BLE001
                self.note(f"Error al renderizar clip: {e}")
        # lo ya evaluado no se vuelve a evaluar; deja margen por si una idea continúa en el siguiente segmento
        self.cursor = max(self.cursor, accepted_end, limit - hi - 2 if not final else limit)
        self.status("recording" if not final else "processing", self.summary())

    def _make_clip(self, c: dict):
        pad0, pad1 = float(self.s["pad_before"]) + 1, float(self.s["pad_after"]) + 1
        segs = [g for g in self.segments if g["end"] > c["start"] - pad0 and g["start"] < c["end"] + pad1]
        if not segs:
            raise media.MediaError("Segmentos del clip no disponibles.")
        inputs = [g["path"] for g in segs]
        pipeline.produce_clip(self.job_id, inputs, len(inputs) > 1, segs[0]["start"], c, self.words, self.s,
                              self.dims, segs[-1]["end"])

    # ------------------------------------------------------------ bucle principal
    def run(self):
        deadline = time.time() + int(self.s["live_wait_minutes"]) * 60
        idx, retries_dead = 0, 0
        try:
            self.status("waiting", "Conectando con el directo…")
            while not self.stop_ev.is_set():
                ok, err = self.start_recorder(idx)
                if not ok:
                    offline = "no está en directo" in err
                    if offline and time.time() < deadline:
                        self.status("waiting", "Esperando a que el canal inicie su directo… (reintento cada 30 s)")
                        self.note("Canal sin directo; reintentando en 30 s.")
                        self._sleep(30)
                        continue
                    raise media.MediaError(err)
                if idx == 0:
                    creators.detect_live_async(self.job_id, self.url, self.s)
                self.note(f"Grabando (tramo {idx + 1}).")
                self.status("recording", self.summary())
                seen: set[str] = set()
                started = time.time()
                while not self.stop_ev.is_set():
                    for name, a, b in self._read_csv(idx, seen):
                        seen.add(name)
                        self.ingest(name, a, b)
                    if not self.alive():
                        break
                    time.sleep(1.5)
                self.kill()
                # último tramo (parcial) que no llegó a registrarse en el csv
                for name, a, b in self._read_csv(idx, seen):
                    seen.add(name)
                    self.ingest(name, a, b)
                self._ingest_partial(idx, seen)
                self.offset = self.timeline_end
                idx += 1
                if self.stop_ev.is_set():
                    break
                if time.time() - started < 30:
                    retries_dead += 1
                else:
                    retries_dead = 0
                if retries_dead >= 3:
                    self.note("La señal se cortó varias veces seguidas: el directo terminó.")
                    break
                self.status("waiting", "Señal interrumpida; reconectando en 15 s…")
                self._sleep(15)
            self.status("processing", "Procesando el final del directo…")
            self.detect(final=True)
            self.status("stopped", f"Directo finalizado · {len(self.clips)} clips · {fmt(self.timeline_end)} grabados")
        except media.MediaError as e:
            self.note(f"ERROR: {e}")
            self.kill()
            self.status("error", str(e))
        except Exception as e:  # noqa: BLE001
            self.note("ERROR: " + traceback.format_exc()[-400:])
            self.kill()
            self.status("error", f"{type(e).__name__}: {e}")
        finally:
            SESSIONS.pop(self.job_id, None)

    def _ingest_partial(self, idx: int, seen: set):
        last = max((g["end"] for g in self.segments if g["path"].name.startswith(f"r{idx}_")), default=self.offset)
        for f in sorted(self.dir.glob(f"r{idx}_*.ts")):
            if f.name in seen:
                continue
            try:
                d = media.probe(f)["duration"]
            except media.MediaError:
                continue
            seen.add(f.name)
            self.ingest(f.name, last - self.offset, last - self.offset + d)

    def _sleep(self, sec: float):
        t = time.time()
        while time.time() - t < sec and not self.stop_ev.is_set():
            time.sleep(0.5)


def start_live(job_id: int):
    sess = LiveSession(job_id)
    SESSIONS[job_id] = sess
    t = threading.Thread(target=sess.run, daemon=True, name=f"live-{job_id}")
    t.start()
    return sess


def stop_live(job_id: int) -> bool:
    sess = SESSIONS.get(job_id)
    if not sess:
        return False
    sess.stop_ev.set()
    sess.kill()
    return True
