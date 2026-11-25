"""Backend ShortsCut propio — FastAPI.

POST /api/jobs  (multipart: file=video.mp4  o  form youtube_url=...)
GET  /api/jobs/{id}      -> estado, progreso, cortes y descargas
GET  /files/{job_id}/{name} -> descarga el MP4 real
GET  /                  -> mini formulario de prueba

Flujo real (sin simulación):
  1. Entrada: video subido, o link de YouTube descargado con yt-dlp.
  2. Transcripción: subtítulos de YouTube (youtube-transcript-api);
     si no hay, Whisper (opcional, ver README).
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
    JOBS[job_id].update(kw)


def get_oembed(video_id):
    try:
        r = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"},
            timeout=15)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def get_transcript(video_id):
    from youtube_transcript_api import YouTubeTranscriptApi
    api = YouTubeTranscriptApi()
    t = api.fetch(video_id, languages=["es", "en"])
    return [{"text": s.text, "start": float(s.start), "duration": float(s.duration)}
            for s in t.snippets]


def download_youtube(url, dest):
    """yt-dlp en el SERVIDOR (no en el navegador).
    En producción usa cookies.txt de una cuenta con sesión
    (ver README) porque YouTube bloquea IPs de centros de datos.
    Formato robusto: mejor video+audio y mezcla a MP4 (el '-f mp4'
    simple falla en muchos videos porque no hay un único mp4).
    Prueba primero el cliente web y luego el cliente Android,
    que a veces pasa el bloqueo de bots; el error incluye la
    salida real de yt-dlp para poder diagnosticarlo."""
    import shutil, sys
    ytdlp = shutil.which("yt-dlp") or str(Path(sys.executable).parent / "yt-dlp")
    cookies = BASE / "cookies.txt"
    base = [ytdlp, "--no-check-certificate", "--no-playlist",
            "--retries", "3", "--fragment-retries", "3",
            "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
            "--merge-output-format", "mp4",
            "-o", str(dest)]
    if cookies.exists():
        base += ["--cookies", str(cookies)]
    attempts = [
        base + [url],
        base + ["--extractor-args", "youtube:player_client=android", url],
        base + ["--extractor-args", "youtube:player_client=ios", url],
    ]
    last_err = ""
    for cmd in attempts:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if r.returncode == 0 and Path(dest).exists():
            return
        last_err = (r.stderr or r.stdout or "").strip()[-600:]
    raise RuntimeError(f"yt-dlp no pudo descargar el video. Último error: {last_err}")


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
        # Sin transcripción aún: en producción aquí va Whisper.
        # Para la prueba, la transcripción se pasa como archivo
        # transcript.json junto al video (ver README).
        transcript = []
        if transcript_file is not None:
            tj = jobdir / "transcript.json"
            with open(tj, "wb") as f:
                shutil.copyfileobj(transcript_file.file, f)
            transcript = json.loads(tj.read_text())
        elif (jobdir / "transcript.json").exists():
            transcript = json.loads((jobdir / "transcript.json").read_text())
        if not transcript:
            JOBS[job_id].update(status="error", step="Error",
                error="Video subido, pero sin transcripción. Sube también transcript.json "
                      "o activa Whisper (ver README). No se inventó ningún corte.")
            return JOBS[job_id]
        threading.Thread(target=run_job, args=(job_id, video_path, transcript, {"n_shorts": n_shorts}), daemon=True).start()
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
                set_status(job_id, status="processing", progress=10, step="Descargando video de YouTube")
                video_path = jobdir / "input.mp4"
                download_youtube(youtube_url, video_path)
                set_status(job_id, progress=30, step="Obteniendo transcripción")
                transcript = get_transcript(vid)
                run_job(job_id, video_path, transcript, {"n_shorts": n_shorts})
            except Exception as e:
                set_status(job_id, status="error", step="Error", error=str(e)[:800])
        threading.Thread(target=_yt, daemon=True).start()
        return JOBS[job_id]

    JOBS[job_id].update(status="error", step="Error",
                        error="Pasa un link de YouTube o sube un video.")
    return JOBS[job_id]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    return JOBS.get(job_id, {"status": "error", "error": "Trabajo no encontrado"})


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
