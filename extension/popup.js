// MeetScribe AI — popup script

const $ = (id) => document.getElementById(id);

function escape(s) {
  return String(s).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
}

async function refreshStatus() {
  const { recording = false } = await chrome.storage.local.get('recording');
  $('ms-status').dataset.on = String(recording);
  $('ms-status-text').textContent = recording ? 'Recording' : 'Idle';
  $('ms-stop-btn').hidden = !recording;
  if (!recording) {
    const box = $('ms-live-box');
    if (!box.textContent.trim() || box.textContent === 'No active meeting.') {
      box.textContent = 'No active meeting.';
    }
  }
}

async function loadHistory() {
  const { meetings = [] } = await chrome.storage.local.get('meetings');
  const list = $('ms-history-list');
  list.innerHTML = '';
  if (!meetings.length) {
    list.innerHTML = '<div class="ms-empty">No meetings recorded yet.</div>';
    return;
  }
  for (const m of meetings) {
    const date = new Date(m.startedAt).toLocaleString();
    const duration = Math.max(0, Math.round((m.endedAt - m.startedAt) / 60000));
    const sum = m.summary && Array.isArray(m.summary.summary) ? m.summary.summary : [];
    const actions =
      m.summary && Array.isArray(m.summary.action_items) ? m.summary.action_items : [];
    const card = document.createElement('div');
    card.className = 'ms-card';
    card.innerHTML = `
      <div class="ms-card-title">${escape(date)} <span class="ms-card-dur">${duration} min</span></div>
      <div class="ms-card-url">${escape(m.url || '')}</div>
      ${sum.length
        ? `<div class="ms-sub">Summary</div><ul>${sum.map((s) => `<li>${escape(s)}</li>`).join('')}</ul>`
        : ''}
      ${actions.length
        ? `<div class="ms-sub">Action items</div><ul>${actions
            .map((a) => `<li>${escape(a.task || JSON.stringify(a))}${a.owner ? ` <em>(${escape(a.owner)})</em>` : ''}</li>`)
            .join('')}</ul>`
        : ''}
      <details><summary>Transcript</summary><pre>${escape(m.transcript || '')}</pre></details>
    `;
    list.appendChild(card);
  }
}

async function loadSettings() {
  const s = await chrome.storage.local.get({
    autoRecord: true,
    backendUrl: 'http://localhost:8000',
    wsUrl: 'ws://localhost:8000/ws/transcribe-live',
  });
  $('ms-auto-record').checked = s.autoRecord;
  $('ms-backend-url').value = s.backendUrl;
  $('ms-ws-url').value = s.wsUrl;
}

$('ms-save-btn').addEventListener('click', async () => {
  await chrome.storage.local.set({
    autoRecord: $('ms-auto-record').checked,
    backendUrl: $('ms-backend-url').value.trim(),
    wsUrl: $('ms-ws-url').value.trim(),
  });
  const btn = $('ms-save-btn');
  btn.textContent = 'Saved ✓';
  setTimeout(() => (btn.textContent = 'Save'), 1500);
});

$('ms-stop-btn').addEventListener('click', () => {
  chrome.runtime.sendMessage({ type: 'MANUAL_STOP' });
});

document.querySelectorAll('.ms-tab').forEach((btn) => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.ms-tab').forEach((t) => t.classList.remove('active'));
    document.querySelectorAll('.ms-panel').forEach((p) => p.classList.remove('active'));
    btn.classList.add('active');
    $(`panel-${btn.dataset.tab}`).classList.add('active');
    if (btn.dataset.tab === 'history') loadHistory();
  });
});

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === 'LIVE_TRANSCRIPT') {
    const box = $('ms-live-box');
    if (box.textContent === 'No active meeting.') box.textContent = '';
    box.textContent += (box.textContent ? ' ' : '') + msg.text;
    box.scrollTop = box.scrollHeight;
  } else if (msg.type === 'RECORDING_STATE_CHANGED' || msg.type === 'HISTORY_UPDATED') {
    refreshStatus();
    loadHistory();
  }
});

refreshStatus();
loadSettings();
loadHistory();
