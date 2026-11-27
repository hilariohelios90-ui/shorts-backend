# Backend de shorts en Render (Docker, plan Free).
# Incluye: Python + FFmpeg + Deno (runtime JS para yt-dlp).
# Si YouTube bloquea la IP de Render, el intento con cookies.txt
# sigue en app.py como respaldo.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg curl unzip git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Deno (binario único): runtime JS para yt-dlp. Se enlaza a
# /usr/local/bin para que yt-dlp lo encuentre siempre (en la
# prueba del 2026-11-26 yt-dlp no detectó el runtime JS).
# Sin servidor bgutil: en esa prueba el PO token recibió 429 y
# el proceso extra contribuyó al corte por memoria (512 MB).
RUN curl -fsSL https://deno.land/install.sh | sh -s -- -y \
    && ln -sf /root/.deno/bin/deno /usr/local/bin/deno \
    && deno --version
ENV PATH="/usr/local/bin:/root/.deno/bin:${PATH}"

WORKDIR /code
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py process.py start.sh ./
RUN mkdir -p work files sfx && chmod +x start.sh

# Render pasa el puerto en $PORT. start.sh levanta uvicorn.
CMD ["./start.sh"]
