# Smart Meeting Notes

An AI-powered meeting transcription tool that converts audio and video recordings into text transcripts. Upload a meeting recording and get a clean `.txt` transcript back — powered by AssemblyAI and FastAPI.

## Purpose

Meetings generate valuable information that's easy to lose. This tool automates the process of converting meeting recordings into readable text, serving as the foundation for extracting summaries, action items, and key decisions.

- Accepts both **audio** (mp3, wav, m4a, flac, ogg, aac, wma) and **video** (mp4, mkv, avi, mov, webm, flv, wmv) files
- Automatically extracts audio from video files using FFmpeg
- Sends audio to AssemblyAI for accurate transcription with automatic language detection
- Returns the transcript as a downloadable `.txt` file

## Installation

### Prerequisites

- Python 3.10+
- FFmpeg
- An [AssemblyAI](https://www.assemblyai.com/) API key

### Setup

1. **Clone the repository**
   ```bash
   git clone https://github.com/your-username/Smart-Meeting-Notes.git
   cd Smart-Meeting-Notes
   ```

2. **Install FFmpeg**
   ```bash
   sudo apt update && sudo apt install ffmpeg -y
   ```

3. **Install Python dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment variables**
   ```bash
   cp .env.example .env
   ```
   Add your AssemblyAI API key to the `.env` file:
   ```
   ASSEMBLYAI_API_KEY=your_api_key_here
   ```

5. **Run the server**
   ```bash
   uvicorn main:app --host 0.0.0.0 --port 8000 --reload
   ```

6. **Test it**
   ```bash
   # Audio file
   curl -X POST -F "file=@meeting.mp3" http://localhost:8000/transcribe --output transcript.txt

   # Video file
   curl -X POST -F "file=@meeting.mp4" http://localhost:8000/transcribe --output transcript.txt
   ```

   API docs available at `http://localhost:8000/docs`

## Tech Stack

- **FastAPI** — Backend framework
- **AssemblyAI** — Speech-to-text transcription
- **FFmpeg** — Audio extraction from video
- **Python-dotenv** — Environment variable management