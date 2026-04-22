# MeetScribe AI — Chrome Extension

Auto-captures Zoom, Google Meet, and Microsoft Teams meetings, streams audio
to the FastAPI backend for live transcription, and runs the LangGraph
summarization pipeline when the meeting ends.

## Install (dev)

1. Open `chrome://extensions`
2. Toggle **Developer mode** (top right)
3. Click **Load unpacked** → select this `extension/` folder
4. The **MeetScribe AI** icon appears in the toolbar

## Configure

Open the popup → **Settings**:

- **Auto-record meetings** — on by default; off disables capture entirely
- **Backend URL** — default `http://localhost:8000`
- **WebSocket URL** — default `ws://localhost:8000/ws/transcribe-live`

## How it works

```
┌────────────────┐    MEETING_STARTED    ┌─────────────────┐
│  content.js    │ ────────────────────▶ │  background.js  │
│ (meet/zoom/    │                       │ (service worker)│
│  teams tab)    │ ◀──────────────────── │                 │
└────────────────┘   RECORDING_STATE     └────────┬────────┘
                                                  │ createDocument
                                                  ▼
                                         ┌─────────────────┐
                                         │  offscreen.js   │
                                         │  • tabCapture   │
                                         │  • MediaRecorder│
                                         │  • WebSocket    │
                                         └────────┬────────┘
                                                  │ 30-s audio chunks
                                                  ▼
                                           ws://…/ws/transcribe-live
                                                  │
                                                  ▼
                                         FastAPI → AssemblyAI realtime
```

When the meeting tab URL no longer matches a meeting pattern (or the tab closes),
`background.js` calls `POST /summarize` with the full transcript and stores the
structured result in `chrome.storage.local`. The popup **History** tab reads it
back.

## Backend endpoint (not included)

The extension assumes a WebSocket endpoint at `ws://localhost:8000/ws/transcribe-live`
that:

1. On connect, receives a JSON `{type:"start", url, ts, mime}` message
2. Then receives binary WebM/Opus audio chunks (~30 s each)
3. Pipes them to AssemblyAI's real-time API
4. Sends `{type:"transcript", text}` messages back for live previews
5. Closes when the client sends `{type:"stop"}`

This endpoint is a next step — the HTTP `/summarize` path is already wired up.

## Files

| File              | Purpose                                                       |
| ----------------- | ------------------------------------------------------------- |
| `manifest.json`   | MV3 manifest — permissions, content scripts, service worker   |
| `content.js`      | Detects meeting URLs, shows floating `REC` badge              |
| `background.js`   | Orchestrator — spawns offscreen doc, `POST /summarize`        |
| `offscreen.js`    | Actual capture: `tabCapture` → `MediaRecorder` → WebSocket    |
| `offscreen.html`  | Host page for `offscreen.js` (required in MV3)                |
| `popup.html/js`   | Toolbar UI: Now / History / Settings tabs                     |
| `styles.css`      | Shared styles (badge + popup)                                 |
