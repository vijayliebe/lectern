#!/bin/sh
# Drops root to PUID:PGID so files in /data are owned by your NAS user.
set -e
set -- uvicorn lectern.api:app --host 0.0.0.0 --port "${PORT:-8000}"
if [ "$(id -u)" = "0" ]; then
  mkdir -p "${LECTERN_DATA_DIR:-/data}" "${HF_HOME:-/data/huggingface}" "${XDG_CACHE_HOME:-/data/cache}"
  chown -R "${PUID}:${PGID}" "${LECTERN_DATA_DIR:-/data}" "${HF_HOME:-/data/huggingface}" "${XDG_CACHE_HOME:-/data/cache}" 2>/dev/null || true
  # HOME is where yt-dlp keeps its cache; give it one the user can write
  export HOME="${LECTERN_DATA_DIR:-/data}"
  exec setpriv --reuid="${PUID}" --regid="${PGID}" --clear-groups "$@"
fi
exec "$@"
