# Backend de shorts en Render (Docker, plan Free).
# Incluye: Python + FFmpeg + Deno (runtime JS para yt-dlp y para el
# servidor de PO tokens bgutil) + servidor POT en 127.0.0.1:4416.
# El PO token deja que yt-dlp parezca haber pasado el chequeo de bot
# sin usar ninguna cuenta de Google. No es garantía: si YouTube igual
# bloquea, el intento con cookies.txt sigue en app.py como respaldo.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg curl unzip git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Deno (binario único): sirve de runtime JS para yt-dlp (--js-runtimes deno)
# y corre el servidor bgutil.
RUN curl -fsSL https://deno.land/install.sh | sh -s -- -y
ENV PATH="/root/.deno/bin:${PATH}"

# Servidor de PO tokens (bgutil-ytdlp-pot-provider, rama estable 2.0.2).
RUN git clone --single-branch --branch 2.0.2 \
      https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil \
    && cd /opt/bgutil/server \
    && deno install --allow-scripts=npm:canvas --frozen

WORKDIR /code
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py process.py start.sh ./
RUN mkdir -p work files sfx && chmod +x start.sh

# Render pasa el puerto en $PORT. start.sh levanta bgutil y uvicorn.
CMD ["./start.sh"]
