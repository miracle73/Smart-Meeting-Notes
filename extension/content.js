// MeetScribe AI — content script
// Detects when user is in a Zoom / Google Meet / Teams meeting,
// notifies the service worker, and shows a floating REC badge.

(() => {
  const PATTERNS = [
    {
      host: 'zoom.us',
      match: (u) => /\/(j|wc|my)\//.test(u.pathname),
    },
    {
      host: 'meet.google.com',
      // Meet URLs look like /abc-defg-hij
      match: (u) => /^\/[a-z]{3}-[a-z]{4}-[a-z]{3}(\/|$)/i.test(u.pathname),
    },
    {
      host: 'teams.microsoft.com',
      match: (u) => /(meetup-join|\/v2\/|\/live\/|\/_#\/meeting)/.test(
        u.pathname + u.hash
      ),
    },
  ];

  function isMeetingUrl() {
    try {
      const u = new URL(location.href);
      const host = u.host.replace(/^www\./, '');
      return PATTERNS.some(
        (p) => (host === p.host || host.endsWith('.' + p.host)) && p.match(u)
      );
    } catch {
      return false;
    }
  }

  let badgeEl = null;
  function showBadge(state) {
    if (!badgeEl) {
      badgeEl = document.createElement('div');
      badgeEl.id = 'meetscribe-badge';
      badgeEl.innerHTML =
        '<span class="ms-dot"></span><span class="ms-label">MeetScribe</span>';
      document.documentElement.appendChild(badgeEl);
    }
    badgeEl.dataset.state = state;
    const label = badgeEl.querySelector('.ms-label');
    if (label) {
      label.textContent =
        state === 'recording'
          ? 'REC'
          : state === 'detected'
          ? 'CLICK ICON'
          : state === 'connecting'
          ? '...'
          : 'MeetScribe';
    }
  }

  function hideBadge() {
    if (badgeEl) badgeEl.remove();
    badgeEl = null;
  }

  let inMeeting = false;

  async function checkState() {
    const should = isMeetingUrl();
    if (should && !inMeeting) {
      inMeeting = true;
      const { autoRecord = true } = await chrome.storage.local.get({
        autoRecord: true,
      });
      if (!autoRecord) return;
      // Chrome blocks automatic tabCapture — the user must click the icon.
      // Badge tells them what to do; background just remembers the tab.
      showBadge('detected');
      chrome.runtime.sendMessage({ type: 'MEETING_STARTED', url: location.href });
    } else if (!should && inMeeting) {
      inMeeting = false;
      hideBadge();
      chrome.runtime.sendMessage({ type: 'MEETING_ENDED' });
    }
  }

  // Watch for SPA navigation (Meet/Teams use pushState)
  let lastUrl = location.href;
  setInterval(() => {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      checkState();
    }
  }, 1200);

  window.addEventListener('beforeunload', () => {
    if (inMeeting) chrome.runtime.sendMessage({ type: 'MEETING_ENDED' });
  });

  // Receive recording-state updates from the background worker
  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type === 'RECORDING_STATE') showBadge(msg.state);
  });

  checkState();
})();
