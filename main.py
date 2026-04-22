import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from urllib.parse import urlparse
from fastapi import FastAPI, File, UploadFile, HTTPException, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from dotenv import load_dotenv
import assemblyai as aai
import httpx

from agents.pipeline import summarize_transcript

load_dotenv()

app = FastAPI(title="Smart Meeting Notes")

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".wma"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv"}
MEDIA_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS

aai.settings.api_key = os.getenv("ASSEMBLYAI_API_KEY")

tasks: dict = {}


class UrlRequest(BaseModel):
    url: str


class SummarizeRequest(BaseModel):
    transcript: str


def extract_audio_from_video(video_path: str) -> str:
    audio_path = video_path.rsplit(".", 1)[0] + ".mp3"
    cmd = ["ffmpeg", "-i", video_path, "-vn", "-acodec", "libmp3lame",
           "-q:a", "2", "-y", audio_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg error: {result.stderr}")
    return audio_path


def transcribe_audio(audio_path: str) -> str:
    config = aai.TranscriptionConfig(
        speech_models=["universal-3-pro", "universal-2"],
        language_detection=True
    )
    transcript = aai.Transcriber(config=config).transcribe(audio_path)
    if transcript.status == "error":
        raise RuntimeError(f"Transcription failed: {transcript.error}")
    return transcript.text


def download_from_url(url: str, tmp_dir: str) -> str:
    """Download media from a URL. Uses yt-dlp for YouTube/streaming, httpx for direct links."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    ext = os.path.splitext(parsed.path.lower())[1]

    use_ytdlp = (
        "youtube.com" in host
        or "youtu.be" in host
        or "drive.google.com" in host
        or ext not in MEDIA_EXTENSIONS
    )

    if use_ytdlp:
        output_tpl = os.path.join(tmp_dir, "download.%(ext)s")
        cmd = ["yt-dlp", "-f", "bestaudio/best", "-o", output_tpl,
               "--no-playlist", url]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"yt-dlp error: {result.stderr}")
        for fname in os.listdir(tmp_dir):
            if fname.startswith("download."):
                return os.path.join(tmp_dir, fname)
        raise RuntimeError("yt-dlp did not produce a file")

    filename = "download" + (ext or ".mp3")
    out_path = os.path.join(tmp_dir, filename)
    with httpx.stream("GET", url, follow_redirects=True, timeout=None) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    return out_path


def run_pipeline(task_id: str, input_path: str, tmp_dir: str, is_video: bool,
                 do_summarize: bool = False):
    try:
        if is_video:
            tasks[task_id]["stage"] = "extracting"
            audio_path = extract_audio_from_video(input_path)
        else:
            audio_path = input_path

        tasks[task_id]["stage"] = "transcribing"
        text = transcribe_audio(audio_path)
        tasks[task_id]["transcript"] = text

        if do_summarize:
            tasks[task_id]["stage"] = "summarizing"
            try:
                result = asyncio.run(summarize_transcript(text))
                tasks[task_id].update(result)
            except Exception as e:
                tasks[task_id]["summary_error"] = str(e)

        tasks[task_id].update({"status": "done", "stage": "done"})
    except Exception as e:
        tasks[task_id].update({"status": "error", "error": str(e)})
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def run_url_pipeline(task_id: str, url: str, tmp_dir: str, do_summarize: bool = False):
    try:
        tasks[task_id]["stage"] = "downloading"
        local_path = download_from_url(url, tmp_dir)
        ext = os.path.splitext(local_path)[1].lower()
        is_video = ext in VIDEO_EXTENSIONS
        run_pipeline(task_id, local_path, tmp_dir, is_video, do_summarize)
    except Exception as e:
        tasks[task_id].update({"status": "error", "error": str(e)})
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    with open("index.html", "r") as f:
        return f.read()


@app.post("/transcribe")
async def transcribe(background_tasks: BackgroundTasks, file: UploadFile = File(...),
                     summarize: bool = False):
    """Upload audio/video → returns task_id. Poll /status/{task_id}.
    Pass ?summarize=true to auto-run the LangGraph summarization pipeline after transcription.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided.")

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in MEDIA_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {ext}. Supported: {sorted(MEDIA_EXTENSIONS)}"
        )

    tmp_dir = tempfile.mkdtemp()
    input_path = os.path.join(tmp_dir, file.filename)
    with open(input_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    task_id = str(uuid.uuid4())
    tasks[task_id] = {"status": "processing", "stage": "queued"}
    background_tasks.add_task(
        run_pipeline, task_id, input_path, tmp_dir, ext in VIDEO_EXTENSIONS, summarize
    )
    return {"task_id": task_id}


@app.post("/transcribe-url")
async def transcribe_url(req: UrlRequest, background_tasks: BackgroundTasks,
                         summarize: bool = False):
    """Transcribe from a URL (YouTube, Google Drive, direct link) → returns task_id.
    Pass ?summarize=true to auto-run the LangGraph summarization pipeline after transcription.
    """
    if not req.url:
        raise HTTPException(status_code=400, detail="Missing 'url' field")

    tmp_dir = tempfile.mkdtemp()
    task_id = str(uuid.uuid4())
    tasks[task_id] = {"status": "processing", "stage": "queued"}
    background_tasks.add_task(run_url_pipeline, task_id, req.url, tmp_dir, summarize)
    return {"task_id": task_id}


@app.post("/summarize")
async def summarize_endpoint(req: SummarizeRequest):
    """Run the multi-agent summarization pipeline on a transcript."""
    if not req.transcript or not req.transcript.strip():
        raise HTTPException(status_code=400, detail="Missing transcript text")
    try:
        return await summarize_transcript(req.transcript)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Summarization failed: {e}")


@app.get("/status/{task_id}")
async def task_status(task_id: str):
    task = tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


REALTIME_SAMPLE_RATE = 16000


@app.websocket("/ws/transcribe-live")
async def ws_transcribe_live(ws: WebSocket):
    """Live transcription WS for the Chrome extension.

    Protocol:
      client -> server: {"type":"start", ...} JSON, then binary WebM/Opus chunks, then {"type":"stop"}
      server -> client: {"type":"transcript", "text":"..."} messages
    """
    await ws.accept()
    loop = asyncio.get_event_loop()
    transcriber = None
    ffmpeg_proc = None
    reader_task = None

    async def send_transcript(text: str):
        try:
            await ws.send_json({"type": "transcript", "text": text})
        except Exception:
            pass

    def on_data(t):
        text = getattr(t, "text", "") or ""
        if text:
            asyncio.run_coroutine_threadsafe(send_transcript(text), loop)

    def on_error(err):
        print(f"AssemblyAI realtime error: {err}")

    async def start_pipeline():
        nonlocal ffmpeg_proc, transcriber, reader_task
        # ffmpeg: read continuous WebM/Opus from stdin, emit 16-kHz mono PCM to stdout
        ffmpeg_proc = subprocess.Popen(
            [
                "ffmpeg", "-loglevel", "quiet", "-i", "pipe:0",
                "-f", "s16le", "-acodec", "pcm_s16le",
                "-ar", str(REALTIME_SAMPLE_RATE), "-ac", "1", "pipe:1",
            ],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        transcriber = aai.RealtimeTranscriber(
            sample_rate=REALTIME_SAMPLE_RATE, on_data=on_data, on_error=on_error
        )
        await loop.run_in_executor(None, transcriber.connect)

        async def pump_pcm():
            while ffmpeg_proc and ffmpeg_proc.stdout:
                chunk = await loop.run_in_executor(None, ffmpeg_proc.stdout.read, 3200)
                if not chunk:
                    break
                try:
                    transcriber.stream(chunk)
                except Exception as e:
                    print(f"realtime stream error: {e}")

        reader_task = asyncio.create_task(pump_pcm())

    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("text") is not None:
                try:
                    data = json.loads(msg["text"])
                except Exception:
                    continue
                if data.get("type") == "start" and ffmpeg_proc is None:
                    await start_pipeline()
                elif data.get("type") == "stop":
                    break
            elif msg.get("bytes") is not None and ffmpeg_proc and ffmpeg_proc.stdin:
                try:
                    await loop.run_in_executor(None, ffmpeg_proc.stdin.write, msg["bytes"])
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"ws_transcribe_live error: {e}")
    finally:
        try:
            if ffmpeg_proc and ffmpeg_proc.stdin:
                ffmpeg_proc.stdin.close()
        except Exception:
            pass
        if reader_task:
            reader_task.cancel()
        try:
            if transcriber:
                await loop.run_in_executor(None, transcriber.close)
        except Exception:
            pass
        try:
            if ffmpeg_proc:
                ffmpeg_proc.terminate()
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass


@app.get("/health")
async def health():
    return {"status": "ok"}
