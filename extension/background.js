// MeetScribe AI — service worker
// Orchestrates tab capture via an offscreen document and stores results.

const DEFAULTS = {
  autoRecord: true,
  backendUrl: 'http://localhost:8000',
  wsUrl: 'ws://localhost:8000/ws/transcribe-live',
};

let activeMeetingTabId = null;

async function getSettings() {
  return await chrome.storage.local.get(DEFAULTS);
}

async function setRecording(on) {
  await chrome.storage.local.set({ recording: on });
  chrome.runtime.sendMessage({ type: 'RECORDING_STATE_CHANGED', on }).catch(() => {});
  if (activeMeetingTabId != null) {
    chrome.tabs
      .sendMessage(activeMeetingTabId, {
        type: 'RECORDING_STATE',
        state: on ? 'recording' : 'idle',
      })
      .catch(() => {});
  }
}

async function ensureOffscreen() {
  if (await chrome.offscreen.hasDocument()) return;
  await chrome.offscreen.createDocument({
    url: 'offscreen.html',
    reasons: ['USER_MEDIA'],
    justification: 'Capture and stream meeting audio for transcription.',
  });
}

async function closeOffscreen() {
  try {
    if (await chrome.offscreen.hasDocument()) await chrome.offscreen.closeDocument();
  } catch (e) {
    console.warn('closeOffscreen failed', e);
  }
}

async function startCaptureWithStreamId(tabId, url, streamId) {
  if (!streamId) {
    console.error('MeetScribe: no streamId — cannot capture tab audio');
    return;
  }
  const settings = await getSettings();
  await ensureOffscreen();
  activeMeetingTabId = tabId;

  chrome.runtime.sendMessage({
    target: 'offscreen',
    type: 'START_CAPTURE',
    streamId,
    url,
    wsUrl: settings.wsUrl,
  });
  await setRecording(true);
}

async function stopCapture() {
  chrome.runtime
    .sendMessage({ target: 'offscreen', type: 'STOP_CAPTURE' })
    .catch(() => {});
  await setRecording(false);
  activeMeetingTabId = null;
  // offscreen will post MEETING_FINISHED; we close the doc after handling it
}

async function handleMeetingFinished(payload) {
  const { url, startedAt, endedAt, transcript } = payload;
  const settings = await getSettings();
  let summary = null;

  if (transcript && transcript.trim()) {
    try {
      const res = await fetch(settings.backendUrl + '/summarize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ transcript }),
      });
      if (res.ok) summary = await res.json();
    } catch (e) {
      console.error('MeetScribe: summarize call failed', e);
    }
  }

  const entry = {
    id: String(Date.now()),
    url,
    startedAt,
    endedAt,
    transcript: transcript || '',
    summary,
  };
  const { meetings = [] } = await chrome.storage.local.get('meetings');
  meetings.unshift(entry);
  await chrome.storage.local.set({ meetings: meetings.slice(0, 50) });

  await closeOffscreen();
  chrome.runtime.sendMessage({ type: 'HISTORY_UPDATED' }).catch(() => {});
}

chrome.runtime.onMessage.addListener((msg, sender) => {
  // Ignore messages addressed to the offscreen document
  if (msg && msg.target === 'offscreen') return;

  if (msg.type === 'MEETING_STARTED') {
    // Meeting detected — DO NOT auto-capture (Chrome blocks tabCapture
    // without a direct user gesture on the extension action). Just remember
    // the tab so the popup can offer a "Start Recording" button.
    chrome.storage.local.set({
      detectedMeeting: { tabId: sender.tab.id, url: msg.url },
    });
  } else if (msg.type === 'MEETING_ENDED') {
    chrome.storage.local.remove('detectedMeeting');
    stopCapture();
  } else if (msg.type === 'MEETING_FINISHED') {
    handleMeetingFinished(msg.payload);
  } else if (msg.type === 'MANUAL_STOP') {
    stopCapture();
  } else if (msg.type === 'START_CAPTURE_FROM_POPUP') {
    // Popup has already called chrome.tabCapture.getMediaStreamId in the
    // user-gesture context, so streamId is valid here.
    startCaptureWithStreamId(msg.tabId, msg.url, msg.streamId);
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  if (activeMeetingTabId === tabId) stopCapture();
});

chrome.runtime.onInstalled.addListener(async () => {
  await chrome.storage.local.set({
    ...DEFAULTS,
    ...(await chrome.storage.local.get(DEFAULTS)),
  });
});
