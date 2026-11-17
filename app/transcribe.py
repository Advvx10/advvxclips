"""Transcripción local con faster-whisper: palabras con tiempos exactos.

`engine` es un objeto reemplazable (los tests inyectan uno falso). La app real usa WhisperEngine.
"""
import concurrent.futures
import os
import tempfile
import threading
from pathlib import Path

from . import media

PRIMERS = {  # pistas de puntuación/acentos para el modelo (mejoran la ortografía)
    "es": "Hola, ¿qué tal? Bienvenidos al directo, gracias por estar aquí.",
    "en": "Hello, how are you? Welcome to the stream, thanks for being here.",
    "pt": "Olá, tudo bem? Bem-vindos à live, obrigado por estarem aqui.",
}


def add_nvidia_dll_dirs():
    """Windows: las DLL de cuBLAS/cuDNN instaladas con pip (paquetes nvidia-*) hay que añadirlas al PATH."""
    import os
    import site
    import sys
    if sys.platform != "win32":
        return
    for base in site.getsitepackages():
        root = os.path.join(base, "nvidia")
        if os.path.isdir(root):
            for d in os.listdir(root):
                b = os.path.join(root, d, "bin")
                if os.path.isdir(b):
                    os.environ["PATH"] = b + os.pathsep + os.environ.get("PATH", "")
                    try:
                        os.add_dll_directory(b)
                    except (OSError, AttributeError):
                        pass


def _cuda_available() -> bool:
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001
        return False


def clean_words(raw: list[dict]) -> list[dict]:
    out, run = [], 0
    for w in raw:
        t = (w.get("w") or "").strip()
        if not t:
            continue
        if out and out[-1]["w"].lower().strip(".,!?¿¡") == t.lower().strip(".,!?¿¡"):
            run += 1
            if run > 3:  # bucle de alucinación de Whisper ("sí sí sí sí sí…")
                continue
        else:
            run = 0
        s = max(float(w["s"]), out[-1]["e"] if out else 0.0)
        e = max(float(w["e"]), s + 0.06)
        out.append({"w": t, "s": round(s, 3), "e": round(e, 3), "p": round(float(w.get("p", 1.0)), 3)})
    return out


class WhisperEngine:
    def __init__(self):
        self._model = None
        self._key = None
        self._lock = threading.Lock()   # un solo transcriptor a la vez (CPU/GPU)
        self._force_cpu = False

    def plan(self, s: dict) -> tuple[str, str, str]:
        device = "cpu" if self._force_cpu else s.get("whisper_device", "auto")
        if device == "auto":
            device = "cuda" if _cuda_available() else "cpu"
        model = s.get("whisper_model", "auto")
        if model == "auto":
            model = "large-v3-turbo" if device == "cuda" else "base"
        return model, device, ("float16" if device == "cuda" else "int8")

    def _load(self, s: dict):
        add_nvidia_dll_dirs()
        from faster_whisper import WhisperModel  # import perezoso: tarda y no hace falta para el resto
        model, device, compute = self.plan(s)
        if self._key == (model, device):
            return
        try:
            self._model = WhisperModel(model, device=device, compute_type=compute)
        except Exception:  # noqa: BLE001  (p. ej. GPU sin librerías CUDA) → CPU
            if device == "cpu":
                raise
            device, compute = "cpu", "int8"
            model = "base" if s.get("whisper_model", "auto") == "auto" else model
            self._model = WhisperModel(model, device=device, compute_type=compute)
        self._key = (model, device)

    def _run(self, wav, kw, offset, total, progress, cancel):
        return _collect_words(self._model, wav, kw, offset, total, progress, cancel)

    def transcribe(self, wav: Path, s: dict, offset: float = 0.0, total: float = 0.0, progress=None,
                   cancel=None) -> dict:
        with self._lock:
            self._load(s)
            lang = None if s.get("language", "auto") == "auto" else s["language"]
            prompt = (s.get("vocabulary") or "").strip()
            if lang in PRIMERS and not prompt:
                prompt = PRIMERS[lang]
            elif lang in PRIMERS:
                prompt = prompt + ". " + PRIMERS[lang]
            kw = _build_kw(lang, prompt)
            try:
                words, info = self._run(wav, kw, offset, total, progress, cancel)
            except media.Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                msg = str(e).lower()
                if not self._force_cpu and any(k in msg for k in ("cublas", "cudnn", "cuda", "dll")):
                    self._force_cpu, self._key = True, None   # GPU mal configurada → CPU automáticamente
                    self._load(s)
                    words, info = self._run(wav, kw, offset, total, progress, cancel)
                else:
                    raise
            words, info = _vad_fallback(self._model, wav, kw, words, info, offset, total, progress, cancel)
            return {"words": clean_words(words), "language": getattr(info, "language", lang or "")}


def _build_kw(lang, prompt: str | None) -> dict:
    return dict(language=lang, beam_size=5, word_timestamps=True, vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 400, "speech_pad_ms": 200},
                condition_on_previous_text=False, initial_prompt=prompt or None)


def _collect_words(model, wav, kw, offset, total, progress, cancel):
    try:
        segs, info = model.transcribe(str(wav), hallucination_silence_threshold=2.0, **kw)
    except TypeError:  # versiones antiguas sin ese parámetro
        segs, info = model.transcribe(str(wav), **kw)
    words = []
    for seg in segs:
        if cancel is not None and cancel.is_set():
            raise media.Cancelled()
        for w in (seg.words or []):
            words.append({"w": w.word, "s": w.start + offset, "e": w.end + offset,
                          "p": getattr(w, "probability", 1.0)})
        if progress and total:
            progress(min(1.0, seg.end / total))
    return words, info


def _vad_fallback(model, wav, kw, words, info, offset, total, progress, cancel):
    """Si el filtro de voz (VAD) casi no encontró nada (p. ej. canto),
    reintenta sin VAD antes de rendirse."""
    if len(words) < 15 and kw.get("vad_filter"):
        kw2 = dict(kw, vad_filter=False)
        words, info = _collect_words(model, wav, kw2, offset, total, progress, cancel)
    return words, info


# ------------------------------------------------------------------ paralelo
# Videos largos en CPU: el audio se parte en trozos y cada trozo se
# transcribe en su propio hilo con su propio modelo. ctranslate2 libera
# el GIL durante la inferencia, así que los hilos sí corren en paralelo
# y el tiempo baja casi en proporción a los núcleos de la CPU.
CHUNK_S = 240.0
_thread_models: threading.local = threading.local()


def _thread_model(model: str, device: str, compute: str):
    key = (model, device, compute)
    cache = getattr(_thread_models, "models", None)
    if cache is None:
        cache = _thread_models.models = {}
    if key not in cache:
        from faster_whisper import WhisperModel  # import perezoso
        cache[key] = WhisperModel(model, device=device, compute_type=compute)
    return cache[key]


def _transcribe_chunk(job: dict) -> tuple[float, list[dict], str]:
    model = _thread_model(job["model"], job["device"], job["compute"])
    kw = job["kw"]
    words, info = _collect_words(model, job["wav"], kw, job["offset"], 0, None, job.get("cancel"))
    words, info = _vad_fallback(model, job["wav"], kw, words, info, job["offset"], 0, None, job.get("cancel"))
    return job["offset"], words, getattr(info, "language", "") or ""


def transcribe_parallel(wav: Path, s: dict, total: float, progress=None, cancel=None) -> dict:
    """Transcribe un audio largo por trozos en paralelo. Devuelve lo mismo que engine.transcribe."""
    model, device, compute = engine.plan(s)
    lang = None if s.get("language", "auto") == "auto" else s["language"]
    prompt = (s.get("vocabulary") or "").strip()
    if lang in PRIMERS and not prompt:
        prompt = PRIMERS[lang]
    elif lang in PRIMERS:
        prompt = prompt + ". " + PRIMERS[lang]
    kw = _build_kw(lang, prompt)
    workers = max(1, min(os.cpu_count() or 2, 4))
    chunks = []
    t = 0.0
    with tempfile.TemporaryDirectory() as td:
        idx = 0
        while t < total:
            dur = min(CHUNK_S, total - t)
            cwav = Path(td) / f"chunk{idx}.wav"
            media.extract_audio(wav, cwav, start=t if t else None, dur=dur)
            chunks.append({"wav": str(cwav), "offset": t, "model": model, "device": device,
                           "compute": compute, "kw": kw, "cancel": cancel})
            t += dur
            idx += 1
        words, lang_out = [], lang or ""
        done = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(_transcribe_chunk, c) for c in chunks]
            for fut in concurrent.futures.as_completed(futs):
                if cancel is not None and cancel.is_set():
                    raise media.Cancelled()
                _off, w, lg = fut.result()
                words.extend(w)
                if lg:
                    lang_out = lg
                done += 1
                if progress:
                    progress(done / len(chunks))
    words.sort(key=lambda w: w["s"])
    return {"words": clean_words(words), "language": lang_out}


engine = WhisperEngine()


def transcribe_media(src, s: dict, offset: float = 0.0, start: float | None = None, dur: float | None = None,
                     progress=None, cancel=None) -> dict:
    """Extrae el audio de `src` (opcionalmente un tramo) y lo transcribe. Tiempos = offset + tiempo en el tramo."""
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "a.wav"
        media.extract_audio(src, wav, start, dur)
        total = dur or media.probe(wav)["duration"]
        if s.get("parallel_transcribe", True) and total > 300 and (os.cpu_count() or 1) >= 4:
            # Audio largo en un PC con 4+ núcleos: por trozos en paralelo.
            # En PCs de 2 núcleos o menos no compensa y se transcribe seguido.
            res = transcribe_parallel(wav, s, total, progress=progress, cancel=cancel)
            if offset:
                for w in res["words"]:
                    w["s"] = round(w["s"] + offset, 3)
                    w["e"] = round(w["e"] + offset, 3)
            return res
        return engine.transcribe(wav, s, offset=offset, total=total, progress=progress, cancel=cancel)
