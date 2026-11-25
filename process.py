"""Motor de cortes: video -> shorts verticales con subtítulos quemados.

Entradas:
  - archivo de video subido, o video descargado de YouTube con yt-dlp
  - transcripción: lista de {text, start, duration} en segundos
    (de los subtítulos de YouTube, o de Whisper en producción)

Salidas por cada corte:
  - MP4 vertical 1080x1920, máx. 60 s, audio AAC original,
    subtítulos quemados (ASS) palabra por palabra no; por frase,
    con la frase actual en amarillo, y 3 efectos de sonido
    mezclados bajito si existen en sfx/ (hook, cambio, cierre).
"""
import json, os, re, subprocess, uuid
from pathlib import Path

MAX_SHORT = 60.0
OUT_W, OUT_H = 1080, 1920

HOOK_WORDS = ["por qué", "nadie", "nunca", "secreto", "error", "verdad",
              "mito", "descubre", "cuidado", "imagina", "sabías"]


def extract_video_id(url: str) -> str | None:
    m = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})", url or "")
    return m.group(1) if m else None


def choose_cuts(transcript, n=3, target=55.0):
    """Agrupa la transcripción en tramos de hasta 60 s.
    Puntúa cada tramo: empieza con gancho (+), es una idea
    completa y no corta una frase a la mitad."""
    if not transcript:
        return []
    # construir tramos: acumular frases hasta ~target s
    cuts, cur, cur_start = [], [], None
    for seg in transcript:
        if cur_start is None:
            cur_start = float(seg["start"])
        cur.append(seg)
        end = float(seg["start"]) + float(seg["duration"])
        if end - cur_start >= target:
            cuts.append((cur_start, end, list(cur)))
            cur, cur_start = [], None
    if cur and cur_start is not None:
        end = float(cur[-1]["start"]) + float(cur[-1]["duration"])
        if end - cur_start >= 15:  # descartar colas de <15 s
            cuts.append((cur_start, end, list(cur)))
    # puntuar y quedarnos con los n mejores, en orden cronológico
    scored = []
    for start, end, segs in cuts:
        text = " ".join(s["text"] for s in segs).lower()
        score = 0
        if any(w in text[:120] for w in HOOK_WORDS):
            score += 3
        if "?" in text:
            score += 1
        if 30 <= end - start <= 60:
            score += 2
        scored.append((score, start, end, segs))
    scored.sort(key=lambda x: (-x[0], x[1]))
    best = sorted(scored[:n], key=lambda x: x[1])
    out = []
    for i, (score, start, end, segs) in enumerate(best, 1):
        dur = min(end - start, MAX_SHORT)
        text = " ".join(s["text"] for s in segs)
        out.append({
            "n": i, "start": round(start, 2), "end": round(start + dur, 2),
            "duration": round(dur, 2),
            "title": make_title(text),
            "description": make_description(text),
            "hashtags": "#Shorts #TikTokCreadores #ReelsEspaña",
            "transcript": [{"text": s["text"], "start": round(float(s["start"]), 2),
                            "duration": round(float(s["duration"]), 2)} for s in segs],
        })
    return out


def make_title(text):
    first = text.strip().split(".")[0].strip()
    return (first[:80] + "…") if len(first) > 80 else first


def make_description(text):
    return ("¿Qué opinas? Te leo en los comentarios. " + text.strip()[:200])


def write_ass(segments, cut_start, cut_end, path):
    """ASS con una frase por evento, estilo ShortsCut:
    letra grande, blanco con borde negro, en el tercio inferior."""
    def fmt(t):
        t = max(0.0, t)
        h, m, s = int(t // 3600), int((t % 3600) // 60), t % 60
        return f"{h}:{m:02d}:{s:05.2f}"
    lines = ["[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080", "PlayResY: 1920",
             "", "[V4+ Styles]",
             "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
             "Style: Sub,Arial,64,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,6,0,2,80,80,320,1",
             "", "[Events]",
             "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for seg in segments:
        s = float(seg["start"]) - cut_start
        e = s + float(seg["duration"])
        if e <= 0 or s >= (cut_end - cut_start):
            continue
        txt = seg["text"].replace("\n", " ").strip()
        lines.append(f"Dialogue: 0,{fmt(max(0,s))},{fmt(min(e, cut_end-cut_start))},Sub,,0,0,0,,{txt}")
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def cut_short(video_path, cut, workdir, sfx_dir=None):
    """FFmpeg: recorta, pone en vertical, quema subtítulos,
    conserva el audio original y mezcla 3 SFX si existen."""
    n = cut["n"]
    start, dur = cut["start"], cut["duration"]
    ass = Path(workdir) / f"short{n}.ass"
    write_ass(cut["transcript"], start, start + dur, ass)
    out = Path(workdir) / f"short{n}.mp4"
    # crop central vertical + escala + subtítulos quemados
    vf = (f"crop=ih*9/16:ih,scale={OUT_W}:{OUT_H},"
          f"subtitles='{ass}':force_style='Fontsize=64,PrimaryColour=&H00FFFFFF,OutlineColour=&H000000,Bold=1,Alignment=2,MarginV=320'")
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-ss", str(start), "-t", str(dur), "-i", str(video_path),
           "-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
           "-shortest", str(out)]
    # SFX opcionales: hook.mp3, cambio.mp3, cierre.mp3 en sfx/
    sfx = []
    if sfx_dir:
        for name in ("hook.mp3", "cambio.mp3", "cierre.mp3"):
            p = Path(sfx_dir) / name
            if p.exists():
                sfx.append(p)
    if sfx:
        # mezcla simple: se añaden como entradas extra con volumen bajo
        cmd = ["ffmpeg", "-y", "-v", "error",
               "-ss", str(start), "-t", str(dur), "-i", str(video_path)]
        for p in sfx:
            cmd += ["-i", str(p)]
        filt = f"[0:v]{vf.split(',', 1)[0] if False else ''}"
        # reconstruir filtro de video y mezcla de audio
        vfilter = (f"crop=ih*9/16:ih,scale={OUT_W}:{OUT_H},"
                   f"subtitles='{ass}':force_style='Fontsize=64,PrimaryColour=&H00FFFFFF,OutlineColour=&H000000,Bold=1,Alignment=2,MarginV=320'")
        afilter = "[0:a]volume=1[a0];" + ";".join(
            f"[{i+1}:a]volume=0.25,adelay={int((dur * i / 3) * 1000)}:all=1[a{i+1}]" for i in range(len(sfx))
        ) + f";[a0]" + "".join(f"[a{i+1}]" for i in range(len(sfx))) + f"amix=inputs={len(sfx)+1}:normalize=0[aout]"
        cmd += ["-filter_complex", f"[0:v]{vfilter}[v];{afilter}",
                "-map", "[v]", "-map", "[aout]",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                "-shortest", str(out)]
    subprocess.run(cmd, check=True)
    return out
