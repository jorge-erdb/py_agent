/**
 * Session history sidebar.
 *
 * /sessions returns only {id, created_at, last_active} — no title, no preview.
 * A list of truncated UUIDs is close to unusable, so previews are derived on the
 * client by reading each session's first user message. That costs one request
 * per session, so results are cached and refreshed lazily.
 */

import * as api from './api.js';
import * as settings from './settings.js';

const el = {
  panel: document.getElementById('history'),
  list: document.getElementById('history-list'),
  toggle: document.getElementById('history-btn'),
  closeBtn: document.getElementById('history-close'),
  scrim: document.getElementById('scrim'),
};

// sessionId -> preview string ('' means "loaded, but the session is empty")
const previews = new Map();

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

/** Drop every cached preview — used when all sessions are destroyed. */
export function clearPreviews() {
  previews.clear();
}

/** Drop a cached preview so the next refresh re-reads it. */
export function invalidatePreview(sessionId) {
  previews.delete(sessionId);
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
  loadPreviews(sessions);
}

function renderNotice(text) {
  const p = document.createElement('p');
  p.className = 'history-notice';
  p.textContent = text;
  el.list.replaceChildren(p);
}

function buildItem(session) {
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
  paintTitle(title, previews.get(session.id));

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
    remove(session.id);
  });

  item.append(open, del);
  return item;
}

/**
 * Fill in previews for any session we have not read yet.
 *
 * Sequential rather than parallel: this is a local backend reading SQLite, and
 * a burst of concurrent full-history reads is a poor trade for a sidebar that
 * fills in over a few hundred milliseconds.
 */
async function loadPreviews(sessions) {
  for (const session of sessions) {
    if (previews.has(session.id)) continue;

    let text = '';
    try {
      const messages = await api.fetchSessionMessages(session.id);
      const first = (messages || []).find((m) => m.role === 'user');
      text = (first?.content || '').trim();
    } catch (err) {
      console.error('Could not preview session:', session.id, err);
      continue; // leave it uncached so a later refresh retries
    }

    previews.set(session.id, text);
    applyPreview(session.id, text);
  }
}

function applyPreview(sessionId, text) {
  const title = el.list.querySelector(`.session[data-id="${CSS.escape(sessionId)}"] .session-title`);
  if (!title) return; // list re-rendered while we were fetching
  paintTitle(title, text);
}

/**
 * Render a cached preview into a title element.
 *
 * The three states are distinct and easy to conflate: `undefined` means not
 * fetched yet, `''` means fetched and the session has no user message, and any
 * other string is the preview. In particular `''` must not be treated as
 * missing — that leaves the row blank on re-render, since the fetch pass skips
 * anything already cached.
 */
function paintTitle(titleEl, cached) {
  const pending = cached === undefined;

  titleEl.textContent = pending ? '…' : cached || 'Empty conversation';
  titleEl.classList.toggle('is-loading', pending);
  titleEl.classList.toggle('is-empty', !pending && !cached);
}

// ── Actions ───────────────────────────────────────────────────────────

async function select(sessionId) {
  if (sessionId === activeId) return close();
  await onSelect(sessionId);
  setActive(sessionId);
  close();
}

async function remove(sessionId) {
  const label = previews.get(sessionId) || sessionId.slice(0, 8);
  if (!confirm(`Delete this conversation permanently?\n\n${label}`)) return;

  try {
    await api.destroySession(sessionId);
  } catch (err) {
    console.error('Could not delete session:', err);
    alert(`Could not delete: ${err.message}`);
    return;
  }

  previews.delete(sessionId);

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
