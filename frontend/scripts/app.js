/**
 * Entry point: owns session state and wires the UI to the API.
 */

import * as api from './api.js';
import * as ui from './ui.js';
import * as history from './history.js';
import * as settings from './settings.js';

let sessionId = null;

/** Point the app at a session, keeping storage and the sidebar in step. */
function setSession(id) {
  sessionId = id;
  if (id) api.storeSessionId(id);
  else api.clearStoredSessionId();
  history.setActive(id);
}

// ── Session lifecycle ─────────────────────────────────────────────────

/**
 * Resolve a usable session on load.
 *
 * A stored id can outlive the session it points at (the DB was wiped, the
 * container was rebuilt), so a 404 is treated as "start fresh" rather than an
 * error worth showing.
 */
async function restoreSession() {
  const stored = api.getStoredSessionId();

  if (stored) {
    try {
      const messages = await api.fetchSessionMessages(stored);
      if (messages !== null) {
        setSession(stored);
        ui.renderHistory(messages);
        return;
      }
      api.clearStoredSessionId();
    } catch (err) {
      console.error('Could not restore session:', err);
      ui.appendMessage({
        role: 'system',
        content: 'Could not reach the backend to restore your last conversation.',
      });
      return;
    }
  }

  // No session yet, and we deliberately do not create one here: doing so on
  // every page load litters the history with empty conversations. send()
  // creates one on demand when there is actually something to say.
  setSession(null);
  ui.clearLog();
}

/**
 * Present an empty chat without touching the server.
 *
 * Nothing is created until the first message is sent, so opening the app or
 * hitting New repeatedly costs no rows in the sessions table.
 */
function resetToNewSession() {
  setSession(null);
  ui.clearLog();
}

/** Create the session server-side. Returns whether it worked. */
async function createSession() {
  try {
    setSession(await api.createSession());
    return true;
  } catch (err) {
    console.error('Could not create session:', err);
    ui.appendMessage({
      role: 'system',
      content: `Could not start a session: ${err.message}`,
    });
    return false;
  }
}

/**
 * Switch the chat to an existing session, or to an empty one when given null
 * (which is what the sidebar does after you delete the conversation you are
 * currently in).
 */
async function switchToSession(id) {
  api.abort();
  ui.setBusy(false);

  if (!id) {
    resetToNewSession();
    return;
  }

  try {
    const messages = await api.fetchSessionMessages(id);
    if (messages === null) {
      // Deleted from under us — the list is stale.
      ui.appendMessage({ role: 'system', content: 'That conversation no longer exists.' });
      history.refresh();
      return;
    }
    setSession(id);
    ui.renderHistory(messages);
  } catch (err) {
    console.error('Could not open session:', err);
    ui.appendMessage({ role: 'system', content: `Could not open conversation: ${err.message}` });
  }
}

// ── Sending a turn ────────────────────────────────────────────────────

async function send(text) {
  // /chat/stream returns messages only — it never tells us which session it
  // used — so the id has to exist before the request goes out.
  if (!sessionId && !(await createSession())) return;

  ui.appendMessage({ role: 'user', content: text });
  ui.setBusy(true, 'Thinking…');

  try {
    for await (const msg of api.sendMessage(text, sessionId)) {
      if (msg._final) break;

      ui.appendMessage(msg);
      ui.setBusy(true, msg.role === 'tool' ? 'Thinking…' : 'Running tools…');
    }
  } catch (err) {
    // An abort is the user pressing Stop — already acknowledged in the log.
    if (err.name !== 'AbortError') {
      console.error('Chat request failed:', err);
      ui.appendMessage({ role: 'system', content: `Request failed: ${err.message}` });
    }
  } finally {
    ui.setBusy(false);
    ui.scrollToBottom();

    // This turn changed the session's last_active, and if it was the first
    // message it also gave the session its preview text.
    history.invalidatePreview(sessionId);
    history.refresh();
  }
}

// ── Event wiring ──────────────────────────────────────────────────────

const form = document.getElementById('composer-form');

form.addEventListener('submit', (event) => {
  event.preventDefault();

  const text = ui.el.input.value.trim();
  if (!text || api.isStreaming()) return;

  ui.el.input.value = '';
  ui.autosize();
  send(text);
});

// Enter sends, Shift+Enter inserts a newline.
ui.el.input.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});

ui.el.input.addEventListener('input', ui.autosize);

document.getElementById('stop-btn').addEventListener('click', () => {
  if (api.abort()) {
    ui.appendMessage({ role: 'system', content: 'Stopped.' });
    ui.setBusy(false);
  }
});

document.getElementById('clear-btn').addEventListener('click', async () => {
  if (!sessionId) return ui.clearLog();

  try {
    await api.clearHistory(sessionId);
    ui.clearLog();
    history.invalidatePreview(sessionId);
    history.refresh();
  } catch (err) {
    console.error('Could not clear history:', err);
    ui.appendMessage({ role: 'system', content: `Could not clear history: ${err.message}` });
  }
});

document.getElementById('new-btn').addEventListener('click', () => {
  api.abort();
  ui.setBusy(false);
  resetToNewSession();
});

// ── Connection indicator ──────────────────────────────────────────────

async function pollHealth() {
  ui.setConnection(await api.checkHealth());
}

// ── Boot ──────────────────────────────────────────────────────────────

history.init({ onSelect: switchToSession });

settings.init({
  // Every session just went away, including the one on screen.
  onSessionsDestroyed: async () => {
    api.abort();
    ui.setBusy(false);
    resetToNewSession();
    history.clearPreviews();
    await history.refresh();
  },
});

pollHealth();
setInterval(pollHealth, 15000);
ui.autosize();

// Restore first so the sidebar knows which session to mark active.
restoreSession().then(() => history.refresh());
