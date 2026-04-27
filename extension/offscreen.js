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
const CHUNK_MS = 1000; // 1-second chunks for live transcript feedback

async function startCapture({ streamId, url, wsUrl }) {
  console.log('[MS] startCapture called', { url, wsUrl, streamIdLen: streamId && streamId.length });
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
    console.log('[MS] getUserMedia OK, tracks=', mediaStream.getTracks().length);
  } catch (e) {
    console.error('[MS] getUserMedia FAILED', e);
    return;
  }

  // Important: route captured audio back to speakers so the user still hears the meeting.
  try {
    audioCtx = new AudioContext();
    audioCtx.createMediaStreamSource(mediaStream).connect(audioCtx.destination);
    console.log('[MS] audio routed to speakers');
  } catch (e) {
    console.error('[MS] audio route failed', e);
  }

  meetingStart = Date.now();
  currentUrl = url;
  transcriptChunks.length = 0;

  console.log('[MS] opening WS:', wsUrl);
  ws = new WebSocket(wsUrl);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    console.log('[MS] WS OPEN — sending start JSON');
    try {
      ws.send(
        JSON.stringify({
          type: 'start',
          url,
          ts: meetingStart,
          mime: 'audio/webm;codecs=opus',
        })
      );
      console.log('[MS] start JSON sent');
    } catch (e) {
      console.error('[MS] start send failed', e);
    }
  };
  ws.onmessage = (e) => {
    console.log('[MS] WS msg:', e.data);
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
  ws.onerror = (e) => console.error('[MS] WS ERROR', e);
  ws.onclose = (e) => console.warn('[MS] WS CLOSED code=' + e.code + ' reason=' + e.reason);

  try {
    recorder = new MediaRecorder(mediaStream, {
      mimeType: 'audio/webm;codecs=opus',
    });
    console.log('[MS] MediaRecorder created');
  } catch (e) {
    console.error('[MS] MediaRecorder ctor failed', e);
    return;
  }
  recorder.ondataavailable = async (e) => {
    console.log('[MS] chunk:', e.data && e.data.size, 'wsState=', ws && ws.readyState);
    if (!e.data || !e.data.size) return;
    if (ws && ws.readyState === WebSocket.OPEN) {
      try {
        const buf = await e.data.arrayBuffer();
        ws.send(buf);
        console.log('[MS] chunk sent', buf.byteLength);
      } catch (err) {
        console.error('[MS] chunk send failed', err);
      }
    }
  };
  recorder.onerror = (e) => console.error('[MS] MediaRecorder error', e);
  recorder.start(CHUNK_MS);
  console.log('[MS] recorder.start() called, state=', recorder.state);
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
