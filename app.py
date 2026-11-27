"""Backend ShortsCut propio — FastAPI.

POST /api/jobs  (multipart: file=video.mp4  o  form youtube_url=...)
GET  /api/jobs/{id}      -> estado, progreso, cortes y descargas
GET  /files/{job_id}/{name} -> descarga el MP4 real
GET  /                  -> mini formulario de prueba

Flujo real (sin simulación):
  1. Entrada: video subido, o link de YouTube.
  2. Para links: primero se baja la transcripción/subtítulos,
     se eligen los mejores tramos y recién entonces yt-dlp
     descarga SOLO esas secciones (no todo el video). Si no
     hay subtítulos, se baja solo el audio para transcribir
     y luego igual se descargan solo las secciones elegidas.
  3. Cortes: process.choose_cuts() — tramos de 30-60 s con gancho.
  4. FFmpeg: corte vertical 1080x1920, subtítulos quemados,
     audio original AAC, SFX mezclados si están en sfx/.
  5. El botón Descargar solo apunta a un MP4 que existe en disco.

Ejecutar:  uvicorn app:app --host 0.0.0.0 --port 8000
"""
import json, os, shutil, subprocess, threading, uuid
from pathlib import Path
import requests
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from process import choose_cuts, cut_short, extract_video_id

BASE = Path(__file__).parent
WORK = BASE / "work"
FILES = BASE / "files"
WORK.mkdir(exist_ok=True)
FILES.mkdir(exist_ok=True)

app = FastAPI(title="Shorts backend propio")
JOBS = {}


def set_status(job_id, **kw):
    job = JOBS.setdefault(job_id, {"id": job_id})
    job.update(kw)
    # Persistir en disco: si la instancia se reinicia (p. ej. por
    # memoria), el sondeo devuelve el último estado real en vez
    # de "Trabajo no encontrado".
    try:
        d = WORK / job_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "job.json").write_text(json.dumps(job, ensure_ascii=False),
                                     encoding="utf-8")
    except Exception:
        pass


def get_oembed(video_id):
    try:
        r = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"},
            timeout=15)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def cookies_path():
    """cookies.txt junto a app.py (desarrollo) o en
    /etc/secrets/cookies.txt (archivo secreto de Render).
    En producción debe ir como secreto: NUNCA en el repo público."""
    p = BASE / "cookies.txt"
    if p.exists():
        return p
    alt = Path("/etc/secrets/cookies.txt")
    return alt if alt.exists() else p


def get_transcript(video_id):
    """Baja subtítulos/transcripción sin bajar el video.
    Si existe cookies.txt, lo usa también para esta petición:
    YouTube puede bloquear la transcripción desde IPs de
    centros de datos igual que bloquea la descarga."""
    from youtube_transcript_api import YouTubeTranscriptApi
    session = None
    cookies = cookies_path()
    if cookies.exists():
        try:
            from http.cookiejar import MozillaCookieJar
            jar = MozillaCookieJar(str(cookies))
            jar.load(ignore_discard=True, ignore_expires=True)
            session = requests.Session()
            session.cookies = jar
        except Exception:
            session = None
    api = YouTubeTranscriptApi(http_client=session) if session else YouTubeTranscriptApi()
    try:
        t = api.fetch(video_id, languages=["es", "en"])
    except Exception:
        # Respaldo: cualquier pista disponible (manual o automática).
        transcripts = api.list(video_id)
        chosen = None
        for tr in transcripts:
            chosen = tr
            break
        if chosen is None:
            raise
        t = chosen.fetch()
    return [{"text": s.text, "start": float(s.start), "duration": float(s.duration)}
            for s in t.snippets if (s.text or "").strip()]


_WHISPER_MODEL = None


def transcribe_whisper(video_path):
    """Transcribe el audio del video con Whisper (faster-whisper).

    Respaldo cuando no hay subtítulos de YouTube ni transcript.json:
    la ruta de subida funciona sola y la ruta de YouTube ya no
    depende de que youtube-transcript-api pase el bloqueo de IP.
    Modelo 'tiny' int8 por defecto (WHISPER_MODEL para cambiarlo):
    es el que cabe cómodo en el plan Free de Render; el modelo se
    descarga la primera vez (~75 MB) y queda en memoria del proceso.
    Devuelve [] si no se detecta voz, sin inventar texto."""
    global _WHISPER_MODEL
    from faster_whisper import WhisperModel
    if _WHISPER_MODEL is None:
        size = os.environ.get("WHISPER_MODEL", "tiny")
        # cpu_threads=1: menos memoria pico en el plan Free (512 MB),
        # donde la prueba del 2026-11-26 se cayó por memoria.
        # local_files_only: el modelo va horneado en la imagen Docker;
        # descargarlo en caliente tras cada reinicio también suma
        # memoria pico. Si no está en local, se descarga como antes.
        try:
            _WHISPER_MODEL = WhisperModel(size, device="cpu",
                                          compute_type="int8", cpu_threads=1,
                                          num_workers=1, local_files_only=True)
        except Exception:
            _WHISPER_MODEL = WhisperModel(size, device="cpu",
                                          compute_type="int8", cpu_threads=1,
                                          num_workers=1)
    # Decodificar con FFmpeg a PCM 16 kHz mono y pasar el arreglo:
    # evita depender de PyAV (su API cambió entre versiones).
    import numpy as np
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(video_path), "-vn",
         "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
        capture_output=True, timeout=600)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError("FFmpeg no pudo extraer el audio del video.")
    audio = np.frombuffer(r.stdout, dtype=np.float32)
    # vad_filter=False: el VAD (onnxruntime) suma ~50 MB de pico
    # medidos localmente; en 512 MB ese margen decide el OOM.
    segments, _info = _WHISPER_MODEL.transcribe(
        audio, vad_filter=False, beam_size=1)
    out = []
    for s in segments:
        text = (s.text or "").strip()
        if text:
            out.append({"text": text, "start": float(s.start),
                        "duration": max(0.1, float(s.end) - float(s.start))})
    # Liberar el modelo antes de cortar con FFmpeg: en 512 MB no
    # pueden convivir Whisper y el corte sin riesgo de OOM.
    del segments, audio
    _WHISPER_MODEL = None
    import gc
    gc.collect()
    return out


def _ytdlp_base(dest, audio_only=False, section=None):
    """Comando base de yt-dlp en el SERVIDOR (no en el navegador).
    YouTube bloquea IPs de centros de datos (Render) con
    "Sign in to confirm you're not a bot". Orden de intentos:
      1. tv: a veces pasa el bloqueo sin cookies.
      2. web con cookies.txt si existe (cuenta desechable,
         exportadas desde ventana privada cerrada al terminar;
         las cookies de una pestaña abierta rotan y dejan de
         servir en horas).
      3. android / ios como último recurso.
    Además pasa --js-runtimes cuando hay deno/node: yt-dlp
    2026 deprecó la extracción sin runtime JS (retos n/sig)
    y sin él faltan formatos aunque no haya bloqueo."""
    import shutil, sys
    ytdlp = shutil.which("yt-dlp") or str(Path(sys.executable).parent / "yt-dlp")
    base = [ytdlp, "--no-check-certificate", "--no-playlist",
            "--retries", "3", "--fragment-retries", "3",
            "-o", str(dest)]
    if audio_only:
        base += ["-f", "ba/bestaudio/b"]
    else:
        base += ["-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
                 "--merge-output-format", "mp4"]
    if section is not None:
        start, end = section
        base += ["--download-sections", f"*{start:.2f}-{end:.2f}",
                 "--force-keyframes-at-cuts"]
    # Runtime JS con ruta explícita: en la prueba del 2026-11-26
    # yt-dlp no detectó Deno solo por el PATH.
    for cand in ("/usr/local/bin/deno", "/root/.deno/bin/deno"):
        if Path(cand).exists():
            base += ["--js-runtimes", f"deno:{cand}"]
            break
    else:
        for runtime in ("deno", "node"):
            if shutil.which(runtime):
                base += ["--js-runtimes", runtime]
                break
    return base


def _run_ytdlp(url, dest, audio_only=False, section=None):
    """Corre yt-dlp con los intentos conocidos. El error incluye
    la salida real de yt-dlp. No descarga todo el video cuando
    section viene definido: solo el tramo pedido."""
    base = _ytdlp_base(dest, audio_only=audio_only, section=section)
    attempts = [base + ["--extractor-args", "youtube:player_client=tv", url]]
    cookies = cookies_path()
    if cookies.exists():
        attempts.append(base + ["--cookies", str(cookies), url])
    attempts += [
        base + ["--extractor-args", "youtube:player_client=android", url],
        base + ["--extractor-args", "youtube:player_client=ios", url],
    ]
    last_err = ""
    for cmd in attempts:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        found = Path(dest)
        if r.returncode == 0 and found.exists():
            return found
        # Con plantillas %(ext)s o secciones, el archivo final
        # puede variar de extensión: buscar por el mismo tallo.
        if r.returncode == 0:
            stem = str(found).replace("%(ext)s", "*")
            matches = sorted(found.parent.glob(Path(stem).name)) if "%(ext)s" in str(dest) else []
            if matches:
                return matches[0]
        last_err = (r.stderr or r.stdout or "").strip()[-600:]
    raise RuntimeError(f"yt-dlp no pudo descargar el video. Último error: {last_err}")


def download_youtube_section(url, start, end, dest):
    """Descarga SOLO el tramo start-end (segundos), no todo el video."""
    return _run_ytdlp(url, dest, section=(float(start), float(end)))


def download_youtube_audio(url, dest_template):
    """Descarga solo el audio, para transcribir cuando el video
    no trae subtítulos. No baja el video completo."""
    return _run_ytdlp(url, dest_template, audio_only=True)


def run_youtube_job(job_id, youtube_url, video_id, n_shorts):
    """Ruta link: transcripción primero, mejores tramos después,
    y descarga de solo las secciones elegidas."""
    jobdir = WORK / job_id
    jobdir.mkdir(exist_ok=True)
    set_status(job_id, status="processing", progress=10,
               step="Obteniendo transcripción del video")
    transcript = []
    transcript_source = "subtitulos_youtube"
    transcript_error = ""
    try:
        transcript = get_transcript(video_id)
    except Exception as e:
        transcript_error = str(e)[:300]
        transcript = []
    if not transcript:
        # Sin subtítulos utilizables: bajar solo audio y transcribir.
        # Si YouTube bloquea también eso, el error real de yt-dlp
        # queda visible y no se inventa ningún corte.
        set_status(job_id, progress=20,
                   step="Sin subtítulos: descargando solo el audio")
        audio_path = download_youtube_audio(youtube_url, jobdir / "audio.%(ext)s")
        set_status(job_id, progress=30,
                   step="Transcribiendo audio con Whisper")
        transcript = transcribe_whisper(audio_path)
        transcript_source = "whisper_solo_audio"
    if not transcript:
        extra = f" Error de subtítulos: {transcript_error}" if transcript_error else ""
        raise RuntimeError(
            "No se obtuvo transcripción ni se detectó voz para "
            "transcribir. No se inventó ningún corte." + extra)
    set_status(job_id, progress=40, step="Eligiendo los mejores momentos")
    cuts = choose_cuts(transcript, n=int(n_shorts))
    if not cuts:
        raise RuntimeError("No se pudo armar ningún corte de 15-60 s con la transcripción.")
    outdir = FILES / job_id
    outdir.mkdir(exist_ok=True)
    results = []
    for i, cut in enumerate(cuts, 1):
        set_status(job_id, progress=40 + int(50 * i / len(cuts)),
                   step=f"Descargando solo el tramo #{i} ({cut['start']}-{cut['end']} s)")
        section_path = download_youtube_section(
            youtube_url, cut["start"], cut["end"], jobdir / f"section{cut['n']}.mp4")
        set_status(job_id, progress=40 + int(50 * i / len(cuts)),
                   step=f"Cortando short #{i} ({cut['duration']} s)")
        # La sección descargada empieza en 0: se ajustan los
        # tiempos para renderizar, conservando los originales
        # en el resultado que ve la app.
        render_cut = {**cut, "start": 0, "end": cut["duration"],
                      "transcript": [
                          {"text": s["text"],
                           "start": round(float(s["start"]) - float(cut["start"]), 2),
                           "duration": s["duration"]}
                          for s in cut["transcript"]]}
        out = cut_short(section_path, render_cut, outdir, sfx_dir=BASE / "sfx")
        results.append({**cut, "file": out.name,
                        "download_url": f"/files/{job_id}/{out.name}",
                        "size_mb": round(out.stat().st_size / 1e6, 2),
                        "transcript_source": transcript_source,
                        "section_only": True})
    set_status(job_id, status="done", progress=100, step="Listo", cuts=results)


def run_job(job_id, video_path, transcript, meta):
    try:
        set_status(job_id, status="processing", progress=40,
                   step="Eligiendo los mejores momentos")
        cuts = choose_cuts(transcript, n=int(meta.get("n_shorts", 3)))
        if not cuts:
            raise RuntimeError("No se pudo armar ningún corte de 15-60 s con la transcripción.")
        outdir = FILES / job_id
        outdir.mkdir(exist_ok=True)
        results = []
        for i, cut in enumerate(cuts, 1):
            set_status(job_id, progress=40 + int(50 * i / len(cuts)),
                       step=f"Cortando short #{i} ({cut['duration']} s)")
            out = cut_short(video_path, cut, outdir, sfx_dir=BASE / "sfx")
            results.append({**cut, "file": out.name,
                            "download_url": f"/files/{job_id}/{out.name}",
                            "size_mb": round(out.stat().st_size / 1e6, 2)})
        set_status(job_id, status="done", progress=100, step="Listo", cuts=results)
    except Exception as e:
        set_status(job_id, status="error", step="Error", error=str(e)[:800])


@app.post("/api/jobs")
async def create_job(youtube_url: str = Form(None), n_shorts: int = Form(3),
                     file: UploadFile = File(None),
                     transcript_file: UploadFile = File(None)):
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"id": job_id, "status": "queued", "progress": 0,
                    "step": "Recibido", "cuts": [], "meta": {}}
    jobdir = WORK / job_id
    jobdir.mkdir(exist_ok=True)

    if file is not None:
        video_path = jobdir / "input.mp4"
        with open(video_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        JOBS[job_id]["meta"] = {"source": "upload", "filename": file.filename}
        # Transcripción: transcript.json si viene; si no, Whisper
        # transcribe el audio del propio video (sin inventar texto).
        transcript = []
        if transcript_file is not None:
            tj = jobdir / "transcript.json"
            with open(tj, "wb") as f:
                shutil.copyfileobj(transcript_file.file, f)
            transcript = json.loads(tj.read_text())
        elif (jobdir / "transcript.json").exists():
            transcript = json.loads((jobdir / "transcript.json").read_text())
        if transcript:
            threading.Thread(target=run_job, args=(job_id, video_path, transcript, {"n_shorts": n_shorts}), daemon=True).start()
            return JOBS[job_id]
        def _upload():
            try:
                set_status(job_id, status="processing", progress=20,
                           step="Transcribiendo audio con Whisper")
                t = transcribe_whisper(video_path)
                if not t:
                    raise RuntimeError(
                        "No se detectó voz en el video subido, así que no hay "
                        "transcripción para armar cortes. Sube un video con voz "
                        "o adjunta transcript.json. No se inventó ningún corte.")
                run_job(job_id, video_path, t, {"n_shorts": n_shorts})
            except Exception as e:
                set_status(job_id, status="error", step="Error", error=str(e)[:800])
        threading.Thread(target=_upload, daemon=True).start()
        return JOBS[job_id]

    if youtube_url:
        vid = extract_video_id(youtube_url)
        if not vid:
            JOBS[job_id].update(status="error", step="Error",
                                error="No se pudo leer el ID del link de YouTube.")
            return JOBS[job_id]
        meta = get_oembed(vid)
        JOBS[job_id]["meta"] = {"source": "youtube", "video_id": vid,
                                "title": meta.get("title"), "author": meta.get("author_name")}
        def _yt():
            try:
                run_youtube_job(job_id, youtube_url, vid, n_shorts)
            except Exception as e:
                set_status(job_id, status="error", step="Error", error=str(e)[:800])
        threading.Thread(target=_yt, daemon=True).start()
        return JOBS[job_id]

    JOBS[job_id].update(status="error", step="Error",
                        error="Pasa un link de YouTube o sube un video.")
    return JOBS[job_id]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    if job_id in JOBS:
        return JOBS[job_id]
    f = WORK / job_id / "job.json"
    if f.exists():
        try:
            job = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            job = {}
        if job.get("status") in ("queued", "processing"):
            return {**job, "status": "error", "step": "Error",
                    "error": "El servidor se reinició durante el trabajo "
                             "(posible falta de memoria). Inténtalo de nuevo."}
        if job:
            return job
    return {"status": "error", "error": "Trabajo no encontrado"}


@app.get("/files/{job_id}/{name}")
def get_file(job_id: str, name: str):
    return FileResponse(FILES / job_id / name, media_type="video/mp4",
                        filename=name)


@app.get("/", response_class=HTMLResponse)
def home():
    return """<h1>Backend de shorts — prueba</h1>
    <form action="/api/jobs" method="post" enctype="multipart/form-data">
      Link YouTube: <input name="youtube_url" size="60"><br><br>
      O sube un video: <input type="file" name="file"><br><br>
      Shorts: <input name="n_shorts" value="3" size="3"><br><br>
      <button>Crear shorts</button>
    </form>
    <p>Luego consulta el estado en /api/jobs/&lt;id&gt; y descarga en /files/&lt;id&gt;/short1.mp4</p>"""
