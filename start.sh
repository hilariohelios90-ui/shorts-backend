#!/bin/sh
# Solo el backend: sin servidor bgutil en segundo plano, para
# dejar la memoria del plan Free (512 MB) a Whisper y FFmpeg.
set -e
cd /code
exec uvicorn app:app --host 0.0.0.0 --port "${PORT:-10000}"
