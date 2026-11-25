# Backend propio de shorts — link de YouTube → MP4 vertical real

Este es el servidor que le falta a tu app de Google AI Studio.
La app web es solo la pantalla; ESTO es lo que descarga, transcribe,
corta, subtitula y entrega el MP4 de verdad (lo que hace ShortsCut).

## Qué hace (sin simular nada)

1. Recibe un link de YouTube o un video subido.
2. Descarga el video con yt-dlp (en el servidor, no en el navegador).
3. Trae título/canal con oEmbed y la transcripción con
   youtube-transcript-api (subtítulos de YouTube, con tiempos).
4. Elige los mejores tramos de 30-60 s (`process.choose_cuts`).
5. Con FFmpeg corta cada tramo, lo pone en vertical 1080x1920,
   le quema los subtítulos (ASS, letra grande, borde negro),
   conserva el audio original en AAC y, si hay archivos en
   `sfx/hook.mp3`, `cambio.mp3`, `cierre.mp3`, los mezcla bajito.
6. Devuelve el MP4 real para reproducir y descargar.

## Probado aquí (2026-11-24, verificado)

- Video de prueba de 75 s con audio → 2 shorts generados:
  `files/test75s/short1.mp4` — 55.0 s, 1080x1920, h264 + AAC,
  subtítulo quemado visible en el fotograma (ver `frame_short1.jpg`).
  `files/test75s/short2.mp4` — 20.0 s.
- API completa probada: subir video + transcripción →
  trabajo `done` al 100% → descarga HTTP 200 del MP4 (3.88 MB).
- La ruta de YouTube, en ESTA máquina, da error honesto:
  YouTube responde 429 / bloquea la IP de este centro de datos
  (lo mismo le pasa a `youtube-transcript-api`: `IpBlocked`).
  El código NO inventa un video en ese caso: el trabajo queda
  en `error` con el motivo. En producción se resuelve como
  se explica abajo.

## Archivos

- `app.py` — API FastAPI: POST /api/jobs, GET /api/jobs/{id}, GET /files/{id}/{mp4}
- `process.py` — elegir cortes, generar ASS, cortar con FFmpeg
- `requirements.txt` — fastapi, uvicorn, yt-dlp, youtube-transcript-api, requests, python-multipart
- `files/test75s/` — ejemplo real de salida (MP4 + .ass)

## Cómo probarlo en tu PC

```bash
cd shortscut_backend
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
# abre http://localhost:8000  y sube un video
# (para subir, acompaña el video con su transcript.json;
#  en producción la transcripción la hace Whisper, ver abajo)
```

## Cómo desplegarlo (para que tu app web lo use de verdad)

Opción fácil: **Render** (o Railway). Sube esta carpeta a GitHub,
crea un Web Service con `pip install -r requirements.txt` y
comando `uvicorn app:app --host 0.0.0.0 --port $PORT`.
En tu app web cambia la URL del backend por la de Render y el
botón "Crear shorts" debe llamar a POST /api/jobs y consultar
GET /api/jobs/{id} cada 5 s hasta que diga `done`.

Opción en un VPS (más barato a largo plazo): un servidor con
2 GB de RAM, ffmpeg instalado, y el mismo comando de arriba
con systemd. Los videos tardan minutos: procesa en segundo
plano (ya está hecho con hilos) y borra los archivos viejos.

## El problema de YouTube y cómo se resuelve en producción

YouTube bloquea descargas desde IPs de centros de datos (429).
ShortsCut lo resuelve con una de estas (o varias):
1. **cookies.txt**: exporta las cookies de tu navegador con
   sesión iniciada en YouTube y pon el archivo junto a `app.py`.
   El código ya lo usa si existe.
2. **Proxy residencial** o un servidor con IP "limpia".
3. **YouTube Data API v3** solo para metadatos, y pedirle al
   usuario que suba el archivo cuando el link falla.
4. **Whisper** para transcribir el audio descargado cuando el
   video no tiene subtítulos: `pip install openai-whisper`
   (necesita más RAM/CPU; mejor usar la API de OpenAI o un
   servicio de transcripción por minutos).

## Costos reales (para que no te sorprenda)

- Servidor Render/Railway: ~$7-20/mes el plan básico.
- Transcripción Whisper API: ~$0.006 por minuto de audio.
- Almacenamiento de MP4: se borran tras 24 h (añadir limpieza).
- Sin esto no hay app que haga shorts reales de un link:
  la IA de la página no puede hacerlo sola.

## Qué falta para igualar a ShortsCut al 100%

- Whisper para videos sin subtítulos.
- Sonidos libres en `sfx/` (hook, cambio, cierre).
- Limpieza automática de archivos viejos y cola de trabajos.
- Títulos/descripciones generados con un LLM a partir de la
  transcripción real (ahora el título sale de la primera frase).
