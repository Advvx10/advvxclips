"""Rutas, binarios y ajustes por defecto."""
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("TBX_DATA") or ROOT / "data")
JOBS_DIR = DATA_DIR / "jobs"
DB_PATH = DATA_DIR / "turbox.db"
FONTS_DIR = ROOT / "fonts"
WEB_DIR = ROOT / "web"
for d in (DATA_DIR, JOBS_DIR):
    d.mkdir(parents=True, exist_ok=True)

IS_WIN = sys.platform == "win32"
NOWIN = {"creationflags": 0x08000000} if IS_WIN else {}  # sin ventanas de consola en Windows
VERSION = "3.1.5"
# PUBLIC=1: el servidor acepta conexiones desde fuera (para el link de tus
# amigos). Sin esto solo acepta 127.0.0.1. La API siempre exige sesión.
PUBLIC = os.environ.get("TBX_PUBLIC") == "1"

# Perfiles de calidad de los clips. fps = fotogramas por segundo de salida,
# crf = calidad de compresión (menor = mejor calidad y archivo más grande),
# preset = velocidad de codificación, whisper = modelo de transcripción
# cuando el modelo está en "auto". "alta" es el perfil por defecto: 60 fps
# y CRF 18 (muy buena calidad). La transcripción sigue siendo la parte
# lenta de un video largo, no el renderizado.
QUALITY_PROFILES = {
    "rapida":     {"fps": 30, "crf": 23, "preset": "veryfast", "whisper": "tiny"},
    "equilibrada": {"fps": 30, "crf": 20, "preset": "veryfast", "whisper": "base"},
    "alta":       {"fps": 60, "crf": 18, "preset": "veryfast", "whisper": "base"},
}


def find_bin(name: str, env: str) -> str:
    if os.environ.get(env):
        return os.environ[env]
    cand = ROOT / "bin" / (name + (".exe" if IS_WIN else ""))
    if cand.exists():
        return str(cand)
    return shutil.which(name) or name


def ffmpeg() -> str:
    return find_bin("ffmpeg", "TBX_FFMPEG")


def ffprobe() -> str:
    return find_bin("ffprobe", "TBX_FFPROBE")


def ytdlp() -> str:
    return find_bin("yt-dlp", "TBX_YTDLP")


DEFAULT_SETTINGS = {
    # Transcripción (Whisper local, con timestamps por palabra)
    "whisper_model": "auto",        # auto | tiny | base | small | medium | large-v3-turbo | large-v3
    "whisper_device": "auto",       # auto | cpu | cuda
    "parallel_transcribe": True,    # videos largos: transcribe por trozos en paralelo (CPU)
    "language": "es",              # es (por defecto; Whisper tiny con "auto" confundía el idioma) | auto | en | pt | ...
    "vocabulary": "",               # nombres propios, jerga del streamer… (mejora la precisión)
    # IA (títulos y puntaje viral)
    "ai_provider": "auto",          # auto | ollama | openai | builtin
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_model": "qwen2.5:3b",
    "openai_base_url": "https://api.groq.com/openai/v1",
    "openai_model": "openai/gpt-oss-20b",  # Groq retiró llama-3.3-70b-versatile para cuentas gratis (404)
    "openai_key": "",
    # Clips
    "clip_min_s": 20, "clip_max_s": 40, "max_clips": 8, "min_score": 55,
    "content_type": "general",  # general | entrevista | podcast | resumen | pelicula | gaming
    "tiktok_mode": True,  # preset TikTok: clips 20-40 s, gancho primero
    "pad_before": 0.25, "pad_after": 0.45,
    "quality_profile": "alta",  # rapida | equilibrada | alta (60 fps, CRF 18)
    # Formato y subtítulos
    "layout": "blur",               # blur (fondo desenfocado) | fill (recorte central) | original | gaming | duo
    "cam_corner": "tl",             # esquina de la cámara en el video original (formato gaming): tl | tr | bl | br
    "watermark_mode": "none",      # none (por defecto, sin marca) | logo | text
    "watermark_text": "ClipsViralesRD_",
    "watermark_position": "br",     # tl | tr | bl | br
    "watermark_size": 220,          # ancho del logo en px
    "watermark_opacity": 80,        # opacidad de la marca de agua, 0-100 (80 = casi sólida)
    "watermark_image": "",          # imagen personalizada subida (DATA_DIR/watermark.png); vacío = logo por defecto
    "captions": True,
    "caption_theme": "impact",
    "caption_position": "low",      # low | center | top
    "loudnorm": True,
    # Descarga
    "cookies_browser": "",          # chrome | edge | firefox | brave | opera  (ayuda con YouTube/Kick)
    "cookies_file": "",
    "max_height": 1080,  # 1080p por defecto (v3.1.4): máxima calidad para el boom de GTA VI; descarga más grande y lenta
    # Live
    "live_segment_s": 120, "live_min_score": 60, "live_max_clips_per_hour": 20,
    "live_wait_minutes": 60,
    # Cuentas sociales (v3.1.3): credenciales de desarrollador, VACÍAS por defecto.
    # Cada plataforma necesita sus propias claves (se pegan en la página Cuentas,
    # solo el administrador). Sin ellas la conexión muestra la guía paso a paso.
    "google_client_id": "", "google_client_secret": "",      # YouTube Data API v3 (Google Cloud)
    "meta_app_id": "", "meta_app_secret": "",                # Facebook + Instagram (Meta for Developers)
    "tiktok_client_key": "", "tiktok_client_secret": "",     # TikTok Content Posting API
    # URL pública de esta app (solo si se publica con TBX_PUBLIC=1). Instagram
    # exige que el video esté en una URL pública para crear el contenedor.
    "social_public_base_url": "",
}
SECRET_KEYS = {"openai_key", "google_client_secret", "meta_app_secret", "tiktok_client_secret"}
