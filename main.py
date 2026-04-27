import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import urlparse
from fastapi import FastAPI, File, UploadFile, HTTPException, BackgroundTasks, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from dotenv import load_dotenv
import assemblyai as aai
import httpx
import websockets
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from agents.pipeline import summarize_transcript
from app.logging import get_logger
from app.metrics import (
    PIPELINE_LATENCY,
    PIPELINE_RESULT,
    WS_SESSIONS,
    WS_TURNS,
    stage_timer,
)

load_dotenv()

log = get_logger(__name__)
app = FastAPI(title="Smart Meeting Notes")


@app.middleware("http")
async def request_logging(request: Request, call_next):
    """Logs every request with status, duration, and a request_id."""
    rid = uuid.uuid4().hex[:8]
    start = time.perf_counter()
    response = None
    try:
        response = await call_next(request)
        return response
    finally:
        dur_ms = int((time.perf_counter() - start) * 1000)
        log.info(
            "http_request",
            request_id=rid,
            method=request.method,
            path=request.url.path,
            status=getattr(response, "status_code", 500),
            duration_ms=dur_ms,
        )

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
        err = (transcript.error or "").lower()
        if "language_detection" in err or "no spoken audio" in err:
            raise RuntimeError(
                "This file has no detectable speech. "
                "Please use a recording that contains spoken audio."
            )
        raise RuntimeError(f"Transcription failed: {transcript.error}")
    return transcript.text or ""


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
        # Invoke yt_dlp via the current interpreter so we don't depend on the
        # `yt-dlp` CLI being on PATH (fixes [WinError 2] on Windows).
        cmd = [sys.executable, "-m", "yt_dlp",
               "-f", "bestaudio/best", "-o", output_tpl,
               "--no-playlist", url]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            stderr = (result.stderr or result.stdout or "").strip()
            low = stderr.lower()
            if "private" in low or "sign in" in low:
                msg = "This video is private or requires sign-in."
            elif "unavailable" in low or "removed" in low:
                msg = "This video is unavailable or has been removed."
            elif "unsupported url" in low:
                msg = "Unsupported URL — please paste a YouTube, Drive, or direct media link."
            elif "http error 403" in low:
                msg = "The video host blocked the download (403)."
            elif "http error 404" in low:
                msg = "The video could not be found (404)."
            else:
                # Keep last line of yt-dlp's error to stay concise
                msg = stderr.splitlines()[-1] if stderr else "yt-dlp failed for unknown reason."
            raise RuntimeError(f"Could not download from URL: {msg}")
        for fname in os.listdir(tmp_dir):
            if fname.startswith("download."):
                return os.path.join(tmp_dir, fname)
        raise RuntimeError("yt-dlp did not produce a file")

    filename = "download" + (ext or ".mp3")
    out_path = os.path.join(tmp_dir, filename)
    try:
        with httpx.stream(
            "GET", url, follow_redirects=True, timeout=60,
            headers={"User-Agent": "Mozilla/5.0 SmartMeetingNotes/1.0"},
        ) as r:
            if r.status_code == 404:
                raise RuntimeError(
                    "The media URL returned 404 (file not found). "
                    "Double-check the link or try another source."
                )
            if r.status_code == 403:
                raise RuntimeError(
                    "The host blocked the download (403 Forbidden). "
                    "Many CDNs reject server-side fetches — try a YouTube link "
                    "or upload the file directly."
                )
            if r.status_code in (502, 503, 504):
                raise RuntimeError(
                    f"The host is temporarily unavailable ({r.status_code}). "
                    "Please retry in a moment or use a different URL."
                )
            r.raise_for_status()
            ctype = r.headers.get("content-type", "").lower()
            if ctype.startswith("text/") or "html" in ctype:
                raise RuntimeError(
                    "The URL returned a web page, not a media file. "
                    "Please paste a direct link to an audio/video file (.mp3, .mp4, .wav, ...) "
                    "or a YouTube URL."
                )
            with open(out_path, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
    except httpx.ConnectError as e:
        # DNS failure / no network / unreachable host
        raise RuntimeError(
            f"Could not reach the host for this URL. "
            f"Check your internet connection or the URL spelling. ({e})"
        )
    except httpx.TimeoutException:
        raise RuntimeError(
            "The download timed out. The host is slow or the file is too large."
        )
    except httpx.HTTPStatusError as e:
        raise RuntimeError(
            f"Download failed with HTTP {e.response.status_code}. "
            "Try a different URL or upload the file directly."
        )
    return out_path


def run_pipeline(task_id: str, input_path: str, tmp_dir: str, is_video: bool,
                 do_summarize: bool = False):
    try:
        if is_video:
            tasks[task_id]["stage"] = "extracting"
            log.info("pipeline_extracting", task_id=task_id)
            with stage_timer("extracting"):
                audio_path = extract_audio_from_video(input_path)
        else:
            audio_path = input_path

        tasks[task_id]["stage"] = "transcribing"
        log.info("pipeline_transcribing", task_id=task_id)
        with stage_timer("transcribing"):
            text = transcribe_audio(audio_path)
        tasks[task_id]["transcript"] = text
        if not text.strip():
            tasks[task_id]["transcript"] = "(No speech detected in this recording.)"

        if do_summarize:
            tasks[task_id]["stage"] = "summarizing"
            log.info("pipeline_summarizing", task_id=task_id)
            try:
                with stage_timer("summarizing"):
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
        log.info("pipeline_downloading", task_id=task_id, url=url)
        with stage_timer("downloading"):
            local_path = download_from_url(url, tmp_dir)
        ext = os.path.splitext(local_path)[1].lower()
        is_video = ext in VIDEO_EXTENSIONS
        run_pipeline(task_id, local_path, tmp_dir, is_video, do_summarize)
    except Exception as e:
        log.warning("pipeline_failed", task_id=task_id, error=str(e))
        tasks[task_id].update({"status": "error", "error": str(e)})
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def metrics():
    """Prometheus scrape endpoint."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


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
AAI_STREAMING_URL = (
    "wss://streaming.assemblyai.com/v3/ws"
    f"?sample_rate={REALTIME_SAMPLE_RATE}"
)


@app.websocket("/ws/transcribe-live")
async def ws_transcribe_live(ws: WebSocket):
    """Live transcription WS for the Chrome extension.

    Bridges: client (WebM/Opus) -> ffmpeg -> PCM -> AssemblyAI v3 Streaming -> client.

    Protocol:
      client -> server: {"type":"start", ...} JSON, then binary WebM/Opus chunks, then {"type":"stop"}
      server -> client: {"type":"transcript", "text":"..."} messages
    """
    await ws.accept()
    WS_SESSIONS.labels(outcome="opened").inc()
    log.info("ws_session_opened")
    loop = asyncio.get_event_loop()
    aai_ws = None
    ffmpeg_proc = None
    pump_task = None
    recv_task = None
    stderr_task = None

    api_key = os.getenv("ASSEMBLYAI_API_KEY") or ""

    async def send_transcript(text: str):
        try:
            await ws.send_json({"type": "transcript", "text": text})
        except Exception:
            pass

    async def start_pipeline():
        nonlocal ffmpeg_proc, aai_ws, pump_task, recv_task, stderr_task
        print("[WS] start_pipeline: spawning ffmpeg + connecting AAI v3")
        debug_wav = os.path.join(tempfile.gettempdir(), f"meetscribe_{uuid.uuid4().hex[:8]}.wav")
        print(f"[WS] dumping captured audio to {debug_wav}")
        ffmpeg_proc = subprocess.Popen(
            [
                "ffmpeg", "-loglevel", "error", "-i", "pipe:0",
                "-map", "0:a:0", "-f", "s16le", "-acodec", "pcm_s16le",
                "-ar", str(REALTIME_SAMPLE_RATE), "-ac", "1", "pipe:1",
                "-map", "0:a:0", "-acodec", "pcm_s16le",
                "-ar", str(REALTIME_SAMPLE_RATE), "-ac", "1", "-y", debug_wav,
            ],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        print(f"[WS] ffmpeg pid={ffmpeg_proc.pid}")

        try:
            aai_ws = await websockets.connect(
                AAI_STREAMING_URL,
                additional_headers={"Authorization": api_key},
                max_size=None,
                open_timeout=30,
                ping_interval=20,
                ping_timeout=20,
            )
            print("[AAI] v3 websocket connected")
        except Exception as e:
            print(f"[AAI] v3 connect failed: {e!r}")
            return

        async def pump_pcm():
            total = 0
            while ffmpeg_proc and ffmpeg_proc.stdout:
                chunk = await loop.run_in_executor(None, ffmpeg_proc.stdout.read, 3200)
                if not chunk:
                    print("[WS] pump_pcm: ffmpeg EOF")
                    break
                total += len(chunk)
                if total % 32000 < 3200:
                    print(f"[WS] pcm -> AAI total={total}")
                try:
                    await aai_ws.send(chunk)
                except Exception as e:
                    print(f"[AAI] send failed: {e!r}")
                    break

        async def recv_aai():
            try:
                async for message in aai_ws:
                    try:
                        data = json.loads(message)
                    except Exception:
                        continue
                    mtype = data.get("type")
                    if mtype == "Begin":
                        print(f"[AAI] Begin session id={data.get('id')}")
                    elif mtype == "Turn":
                        text = data.get("transcript", "")
                        end = data.get("end_of_turn", False)
                        log.debug("aai_turn", end_of_turn=end, chars=len(text))
                        if end:
                            WS_TURNS.inc()
                        if text:
                            await send_transcript(text)
                    elif mtype == "Termination":
                        print(f"[AAI] Termination: {data}")
                    else:
                        print(f"[AAI] msg: {data}")
            except Exception as e:
                print(f"[AAI] recv loop ended: {e!r}")

        async def drain_ffmpeg_stderr():
            while ffmpeg_proc and ffmpeg_proc.stderr:
                line = await loop.run_in_executor(None, ffmpeg_proc.stderr.readline)
                if not line:
                    break
                try:
                    print(f"[ffmpeg] {line.decode(errors='replace').rstrip()}")
                except Exception:
                    pass

        pump_task = asyncio.create_task(pump_pcm())
        recv_task = asyncio.create_task(recv_aai())
        stderr_task = asyncio.create_task(drain_ffmpeg_stderr())

    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                print(f"[WS] disconnect code={msg.get('code')}")
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
                b = msg["bytes"]
                try:
                    await loop.run_in_executor(None, ffmpeg_proc.stdin.write, b)
                except Exception as e:
                    print(f"[WS] ffmpeg stdin write error: {e!r}")
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
        for t in (pump_task, recv_task, stderr_task):
            if t:
                t.cancel()
        try:
            if aai_ws:
                await aai_ws.send(json.dumps({"type": "Terminate"}))
                await aai_ws.close()
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
