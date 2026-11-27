#!/bin/sh
# Levanta el servidor de PO tokens (bgutil) en segundo plano y luego
# el backend. Si bgutil falla, el backend igual arranca: yt-dlp
# seguirá con los demás intentos (tv, cookies, android, ios).
set -e
cd /opt/bgutil/server/node_modules
deno run --allow-env --allow-net --allow-ffi=. --allow-read=. ../src/main.ts --port 4416 &
cd /code
exec uvicorn app:app --host 0.0.0.0 --port "${PORT:-10000}"
