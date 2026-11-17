# AdvvxClips — imagen lista para un servidor (Render plan gratis u otro).
# Render gratis: 512 MB de RAM, CPU compartida, se duerme a los 15 min sin
# uso y el disco es temporal (los clips y la base se borran al reiniciar).
# Por eso esta imagen usa el perfil Rápida (Whisper tiny) por defecto en
# el servidor: es el modelo que menos memoria pide.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TBX_PUBLIC=1 \
    TBX_DATA=/data

# ffmpeg + Node.js (el runtime JS que yt-dlp necesita para YouTube)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl ca-certificates gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt \
 && pip install "yt-dlp>=2024.1.1"

COPY app app
COPY web web
COPY fonts fonts
COPY assets assets

# Descarga el modelo tiny de Whisper en la imagen, para que el primer
# trabajo no tenga que bajarlo (en el plan gratis el arranque ya es lento).
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('tiny', device='cpu', compute_type='int8')"

RUN mkdir -p /data
EXPOSE 8765
CMD ["sh", "-c", "python -m app --host 0.0.0.0 --port ${PORT:-8765} --no-browser"]
