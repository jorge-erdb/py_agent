/**
 * Session history sidebar.
 *
 * /sessions returns {id, created_at, last_active, preview}, where `preview` is
 * the session's first user message truncated server-side. The whole list
 * therefore arrives in one request — no per-session fetching, and no cache to
 * keep in step with the server.
 */

import * as api from './api.js';
import * as settings from './settings.js';
import * as ui from './ui.js';

const el = {
  panel: document.getElementById('history'),
  list: document.getElementById('history-list'),
  toggle: document.getElementById('history-btn'),
  closeBtn: document.getElementById('history-close'),
  scrim: document.getElementById('scrim'),
};

let activeId = null;
let onSelect = null;

/**
 * @param {object} handlers
 * @param {(id: string) => Promise<void>} handlers.onSelect  switch to a session
 */
export function init(handlers) {
  onSelect = handlers.onSelect;

  el.toggle.addEventListener('click', toggle);
  el.closeBtn.addEventListener('click', dismiss);
  el.scrim.addEventListener('click', close);

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && isOpen()) close();
  });
}

// ── Panel visibility ──────────────────────────────────────────────────
// The sidebar behaves differently either side of the breakpoint: docked and
// retractable on wide screens, a slide-over drawer on narrow ones. The `open`
// class drives only the drawer; retraction is a persisted preference.

/** Matches the breakpoint in main.css where the sidebar undocks. */
function isDocked() {
  return window.matchMedia('(min-width: 901px)').matches;
}

function isOpen() {
  return el.panel.classList.contains('open');
}

/** The ☰ button: retract/expand when docked, open/close the drawer when not. */
export function toggle() {
  if (isDocked()) {
    const collapsing = !settings.get('sidebarCollapsed');
    settings.set('sidebarCollapsed', collapsing);
    if (!collapsing) refresh();
    return;
  }

  el.panel.classList.toggle('open');
  el.scrim.hidden = !isOpen();
  if (isOpen()) refresh();
}

/**
 * Close the drawer. Deliberately a no-op when docked — selecting a session
 * calls this, and retracting the sidebar every time you picked a conversation
 * would be hostile.
 */
export function close() {
  el.panel.classList.remove('open');
  el.scrim.hidden = true;
}

/** The × inside the panel: retract when docked, otherwise close the drawer. */
function dismiss() {
  if (isDocked()) settings.set('sidebarCollapsed', true);
  else close();
}

/** Mark which session is current, without refetching the list. */
export function setActive(sessionId) {
  activeId = sessionId;
  for (const item of el.list.querySelectorAll('.session')) {
    item.classList.toggle('active', item.dataset.id === sessionId);
  }
}

// ── Rendering ─────────────────────────────────────────────────────────

/** Fetch the session list and re-render. */
export async function refresh() {
  let sessions;
  try {
    sessions = await api.fetchSessions();
  } catch (err) {
    console.error('Could not load sessions:', err);
    renderNotice('Could not load sessions.');
    return;
  }

  if (!sessions.length) {
    renderNotice('No conversations yet.');
    return;
  }

  el.list.replaceChildren(...sessions.map(buildItem));
}

function renderNotice(text) {
  const p = document.createElement('p');
  p.className = 'history-notice';
  p.textContent = text;
  el.list.replaceChildren(p);
}

function buildItem(session) {
  const preview = (session.preview || '').trim();

  const item = document.createElement('div');
  item.className = 'session';
  item.dataset.id = session.id;
  if (session.id === activeId) item.classList.add('active');

  // The row is a button so it is keyboard-reachable; the delete control sits
  // beside it rather than inside it (nested buttons are invalid).
  const open = document.createElement('button');
  open.className = 'session-open';
  open.type = 'button';

  const title = document.createElement('span');
  title.className = 'session-title';
  // A session with no user message yet still needs a label.
  title.textContent = preview || 'Empty conversation';
  title.classList.toggle('is-empty', !preview);

  const meta = document.createElement('span');
  meta.className = 'session-meta';
  meta.textContent = formatTime(session.last_active);
  meta.title = new Date(session.last_active * 1000).toLocaleString();

  open.append(title, meta);
  open.addEventListener('click', () => select(session.id));

  const del = document.createElement('button');
  del.className = 'session-delete';
  del.type = 'button';
  del.title = 'Delete this conversation';
  del.setAttribute('aria-label', `Delete conversation ${session.id.slice(0, 8)}`);
  del.textContent = '×';
  del.addEventListener('click', (event) => {
    event.stopPropagation();
    remove(session.id, preview);
  });

  item.append(open, del);
  return item;
}

// ── Actions ───────────────────────────────────────────────────────────

async function select(sessionId) {
  if (sessionId === activeId) return close();
  await onSelect(sessionId);
  setActive(sessionId);
  close();
}

async function remove(sessionId, preview) {
  const label = preview || sessionId.slice(0, 8);

  const confirmed = await ui.confirmAction({
    title: 'Delete this conversation?',
    body: `${label}\n\nThis cannot be undone.`,
    confirmLabel: 'Delete',
  });
  if (!confirmed) return;

  try {
    await api.destroySession(sessionId);
  } catch (err) {
    console.error('Could not delete session:', err);
    ui.appendMessage({
      role: 'system',
      content: `Could not delete that conversation: ${err.message}`,
    });
    return;
  }

  // Deleting the conversation you are looking at leaves the chat pointing at
  // something that no longer exists — hand control back to the app to start a
  // fresh one.
  if (sessionId === activeId) {
    await onSelect(null);
  }

  await refresh();
}

// ── Formatting ────────────────────────────────────────────────────────

/** Epoch seconds -> compact relative label. */
function formatTime(epochSeconds) {
  const then = epochSeconds * 1000;
  const diff = Date.now() - then;

  const minute = 60_000;
  const hour = 60 * minute;
  const day = 24 * hour;

  if (diff < minute) return 'just now';
  if (diff < hour) return `${Math.floor(diff / minute)}m ago`;
  if (diff < day) return `${Math.floor(diff / hour)}h ago`;
  if (diff < 7 * day) return `${Math.floor(diff / day)}d ago`;

  return new Date(then).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}
