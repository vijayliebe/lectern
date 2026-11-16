# Lectern: transcripts for local video files (subtitle files -> YouTube captions -> speech-to-text).
FROM denoland/deno:bin AS deno

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LECTERN_DATA_DIR=/data \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HUB_DISABLE_XET=1 \
    HF_HOME=/data/huggingface \
    XDG_CACHE_HOME=/data/cache \
    PUID=1000 \
    PGID=1000 \
    PORT=8000

# yt-dlp needs a JavaScript runtime for YouTube; deno is the one it supports by default
COPY --from=deno /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY lectern ./lectern
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /data /media

VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/v1/health',timeout=4)" || exit 1
ENTRYPOINT ["/entrypoint.sh"]
