// MeetScribe AI — offscreen document
// Does the actual tab-audio capture, chunking, and WebSocket streaming.
// (Service workers in MV3 can't touch MediaRecorder / getUserMedia directly.)

let mediaStream = null;
let recorder = null;
let ws = null;
let audioCtx = null;
let meetingStart = null;
let currentUrl = null;
const transcriptChunks = [];
const CHUNK_MS = 30000; // 30-second chunks

async function startCapture({ streamId, url, wsUrl }) {
  if (mediaStream) stopCapture(false); // guard against double-start

  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        mandatory: {
          chromeMediaSource: 'tab',
          chromeMediaSourceId: streamId,
        },
      },
      video: false,
    });
  } catch (e) {
    console.error('MeetScribe: getUserMedia failed', e);
    return;
  }

  // Important: route captured audio back to speakers so the user still hears the meeting.
  audioCtx = new AudioContext();
  audioCtx.createMediaStreamSource(mediaStream).connect(audioCtx.destination);

  meetingStart = Date.now();
  currentUrl = url;
  transcriptChunks.length = 0;

  ws = new WebSocket(wsUrl);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    try {
      ws.send(
        JSON.stringify({
          type: 'start',
          url,
          ts: meetingStart,
          mime: 'audio/webm;codecs=opus',
        })
      );
    } catch {}
  };
  ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === 'transcript' && msg.text) {
        transcriptChunks.push(msg.text);
        chrome.runtime
          .sendMessage({ type: 'LIVE_TRANSCRIPT', text: msg.text })
          .catch(() => {});
      }
    } catch {}
  };
  ws.onerror = (e) => console.warn('MeetScribe: WS error', e);

  recorder = new MediaRecorder(mediaStream, {
    mimeType: 'audio/webm;codecs=opus',
  });
  recorder.ondataavailable = async (e) => {
    if (!e.data || !e.data.size) return;
    if (ws && ws.readyState === WebSocket.OPEN) {
      try {
        const buf = await e.data.arrayBuffer();
        ws.send(buf);
      } catch (err) {
        console.warn('MeetScribe: chunk send failed', err);
      }
    }
  };
  recorder.start(CHUNK_MS);
}

function stopCapture(emitFinished = true) {
  try {
    if (recorder && recorder.state !== 'inactive') recorder.stop();
  } catch {}
  try {
    mediaStream && mediaStream.getTracks().forEach((t) => t.stop());
  } catch {}
  try {
    audioCtx && audioCtx.close();
  } catch {}
  try {
    if (ws) {
      if (ws.readyState === WebSocket.OPEN)
        ws.send(JSON.stringify({ type: 'stop' }));
      ws.close();
    }
  } catch {}

  const payload = {
    url: currentUrl,
    startedAt: meetingStart,
    endedAt: Date.now(),
    transcript: transcriptChunks.join(' ').trim(),
  };

  mediaStream = null;
  recorder = null;
  ws = null;
  audioCtx = null;
  currentUrl = null;
  meetingStart = null;

  if (emitFinished) {
    chrome.runtime
      .sendMessage({ type: 'MEETING_FINISHED', payload })
      .catch(() => {});
  }
}

chrome.runtime.onMessage.addListener((msg) => {
  if (!msg || msg.target !== 'offscreen') return;
  if (msg.type === 'START_CAPTURE') startCapture(msg);
  else if (msg.type === 'STOP_CAPTURE') stopCapture(true);
});
