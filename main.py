import os
import shutil
import subprocess
import tempfile
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import FileResponse
from dotenv import load_dotenv
import assemblyai as aai

load_dotenv()

app = FastAPI(title="Meeting Summarizer - Transcription Service")

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".wma"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv"}

aai.settings.api_key = os.getenv("ASSEMBLYAI_API_KEY")


def extract_audio_from_video(video_path: str) -> str:
    """Extract audio from video using FFmpeg."""
    audio_path = video_path.rsplit(".", 1)[0] + ".mp3"
    cmd = [
        "ffmpeg", "-i", video_path,
        "-vn", "-acodec", "libmp3lame", "-q:a", "2",
        "-y", audio_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg error: {result.stderr}")
    return audio_path


def transcribe_audio(audio_path: str) -> str:
    """Send audio to AssemblyAI and return transcript text."""
    config = aai.TranscriptionConfig(
        speech_models=["universal-3-pro", "universal-2"],
        language_detection=True
    )
    transcript = aai.Transcriber(config=config).transcribe(audio_path)
    if transcript.status == "error":
        raise RuntimeError(f"Transcription failed: {transcript.error}")
    return transcript.text


@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...)):
    """Upload audio or video file → returns a .txt transcript."""

    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided.")

    ext = os.path.splitext(file.filename)[1].lower()

    if ext not in AUDIO_EXTENSIONS and ext not in VIDEO_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {ext}. Supported: {AUDIO_EXTENSIONS | VIDEO_EXTENSIONS}"
        )

    # Save uploaded file to temp directory
    tmp_dir = tempfile.mkdtemp()
    input_path = os.path.join(tmp_dir, file.filename)

    try:
        with open(input_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        # If video, extract audio first
        if ext in VIDEO_EXTENSIONS:
            audio_path = extract_audio_from_video(input_path)
        else:
            audio_path = input_path

        # Transcribe with AssemblyAI
        transcript_text = transcribe_audio(audio_path)

        # Save transcript to .txt file
        output_path = os.path.join(tmp_dir, "transcript.txt")
        with open(output_path, "w") as f:
            f.write(transcript_text)

        return FileResponse(
            path=output_path,
            filename="transcript.txt",
            media_type="text/plain"
        )

    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")


@app.get("/health")
async def health():
    return {"status": "ok"}