/**
 * DOM rendering. Nothing in here talks to the network.
 *
 * Two message shapes reach this module and they are not identical:
 *
 *   live stream  — assistant messages may carry a `tool_calls` array
 *   restored     — persisted rows have `tool_name`/`command` but no `tool_calls`,
 *                  and assistant `content` may be null
 *
 * So rendering keys off `role` plus the flat `tool_name`/`command` fields, which
 * are present in both.
 */

import * as settings from './settings.js';

const el = {
  log: document.getElementById('log'),
  empty: document.getElementById('empty'),
  status: document.getElementById('status'),
  statusText: document.getElementById('status-text'),
  conn: document.getElementById('conn'),
  sendBtn: document.getElementById('send-btn'),
  stopBtn: document.getElementById('stop-btn'),
  input: document.getElementById('input'),
};

export { el };

const confirmEl = {
  dialog: document.getElementById('confirm'),
  title: document.getElementById('confirm-title'),
  body: document.getElementById('confirm-body'),
  ok: document.getElementById('confirm-ok'),
  cancel: document.getElementById('confirm-cancel'),
};

// ── Confirmation ──────────────────────────────────────────────────────

/**
 * Ask the user to confirm a destructive action.
 *
 * Replaces window.confirm(), which cannot be styled, blocks the event loop, and
 * looked nothing like the typed-DELETE confirmation the settings dialog already
 * uses for the same class of action.
 *
 * Cancel is focused on open and Escape resolves false, so the safe answer is
 * always the default one.
 *
 * @returns {Promise<boolean>} whether the user confirmed
 */
export function confirmAction({ title, body = '', confirmLabel = 'Delete' }) {
  const dialog = confirmEl.dialog;

  confirmEl.title.textContent = title;
  confirmEl.body.textContent = body;
  confirmEl.ok.textContent = confirmLabel;

  return new Promise((resolve) => {
    let settled = false;

    const finish = (result) => {
      if (settled) return; // `close` fires after a button too
      settled = true;

      confirmEl.ok.removeEventListener('click', onOk);
      confirmEl.cancel.removeEventListener('click', onCancel);
      dialog.removeEventListener('close', onCancel);
      dialog.removeEventListener('click', onBackdrop);

      if (dialog.open) dialog.close();
      resolve(result);
    };

    const onOk = () => finish(true);
    const onCancel = () => finish(false); // also the Escape path, via `close`
    const onBackdrop = (event) => {
      if (event.target === dialog) finish(false);
    };

    confirmEl.ok.addEventListener('click', onOk);
    confirmEl.cancel.addEventListener('click', onCancel);
    dialog.addEventListener('close', onCancel);
    dialog.addEventListener('click', onBackdrop);

    dialog.showModal();
    confirmEl.cancel.focus();
  });
}

// ── Markdown ──────────────────────────────────────────────────────────

if (window.marked) {
  window.marked.setOptions({ breaks: true, gfm: true });
}

/**
 * Render markdown to sanitized HTML.
 *
 * Agent output is untrusted — it can echo back anything a tool read off disk —
 * so it always goes through DOMPurify. If either library failed to load we fall
 * back to escaped plain text rather than injecting raw HTML.
 */
function renderMarkdown(text) {
  const source = String(text ?? '');
  if (!window.marked || !window.DOMPurify) {
    return escapeHTML(source);
  }
  return window.DOMPurify.sanitize(window.marked.parse(source));
}

function escapeHTML(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

// ── Message rendering ─────────────────────────────────────────────────

/**
 * Append a message to the log.
 * Returns the created element, or null if the message had nothing to show.
 */
export function appendMessage(msg) {
  const node = buildMessage(msg);
  if (!node) return null;

  hideEmptyState();
  el.log.appendChild(node);
  scrollToBottom();
  return node;
}

function buildMessage(msg) {
  if (!msg) return null;

  if (msg.role === 'tool') return buildToolBlock(msg);
  if (msg.role === 'user') return buildBubble('user', 'You', msg.content);
  if (msg.role === 'system') return buildBubble('system', 'System', msg.content);

  // Assistant turns that only exist to carry tool calls have no prose worth
  // showing — the tool block that follows tells the story. The two sources
  // disagree on what such a turn looks like: the live stream sends empty
  // content, while the persisted row stores the serialized tool_calls array as
  // its content. Rendering that row would dump raw JSON into the log on reload.
  if (msg.message_type === 'assistant_tool_call') return null;

  const content = (msg.content ?? '').trim();
  if (!content) return null;
  return buildBubble('assistant', 'Agent', content);
}

function buildBubble(kind, label, content) {
  const wrap = document.createElement('article');
  wrap.className = `msg msg-${kind}`;

  const head = document.createElement('div');
  head.className = 'msg-role';
  head.textContent = label;

  const body = document.createElement('div');
  body.className = 'msg-body';

  if (kind === 'user') {
    // User text is shown verbatim — no markdown, no surprises.
    body.textContent = content ?? '';
  } else {
    body.innerHTML = renderMarkdown(content);
  }

  wrap.append(head, body);
  return wrap;
}

/**
 * Tool results collapse by default — they are frequently long (file dumps,
 * command output) and would otherwise bury the conversation.
 */
function buildToolBlock(msg) {
  const details = document.createElement('details');
  details.className = 'tool';
  details.open = settings.get('toolsExpanded');

  const summary = document.createElement('summary');
  summary.className = 'tool-summary';

  // Mark blocks the user opened or closed by hand, so changing the default in
  // Settings does not yank them back. This listens for the summary click rather
  // than the details 'toggle' event: `toggle` also fires for the programmatic
  // `open` assignments above and in settings.apply(), which would mark every
  // block as touched. Keyboard activation of a <summary> dispatches a click
  // too, so this still covers non-mouse users.
  summary.addEventListener('click', () => {
    details.dataset.touched = '';
  });

  const name = document.createElement('span');
  name.className = 'tool-name';
  name.textContent = msg.tool_name || 'tool';

  const subtitle = document.createElement('code');
  subtitle.className = 'tool-command';
  subtitle.textContent = msg.command || '';

  summary.append(name, subtitle);

  const output = document.createElement('pre');
  output.className = 'tool-output';
  output.textContent = msg.content ?? '';

  details.append(summary, output);
  return details;
}

// ── Log state ─────────────────────────────────────────────────────────

export function renderHistory(messages) {
  clearLog();
  for (const msg of messages) appendMessage(msg);
}

export function clearLog() {
  el.log.replaceChildren(el.empty);
  showEmptyState();
}

function hideEmptyState() {
  el.empty.hidden = true;
}

function showEmptyState() {
  el.empty.hidden = false;
}

export function scrollToBottom() {
  el.log.scrollTop = el.log.scrollHeight;
}

// ── Status & controls ─────────────────────────────────────────────────

export function setBusy(busy, text = 'Working…') {
  el.status.hidden = !busy;
  el.statusText.textContent = text;
  el.log.setAttribute('aria-busy', String(busy));

  el.sendBtn.hidden = busy;
  el.stopBtn.hidden = !busy;
  el.input.disabled = busy;

  if (!busy && canTakeFocus()) el.input.focus();
}

/**
 * Whether returning focus to the composer would steal it from somewhere.
 *
 * A turn can finish at any moment, including while the settings dialog is open
 * or the user is tabbing the sidebar. Focusing unconditionally yanks them out
 * of whatever they were doing — and out of a modal, which is worse than merely
 * rude for keyboard and screen-reader users.
 *
 * Disabling the textarea during a turn also blurs it, so after a normal send
 * the active element is <body> and this returns true.
 */
function canTakeFocus() {
  const active = document.activeElement;
  return !active || active === document.body || active === el.input;
}

export function setConnection(online) {
  el.conn.dataset.state = online ? 'online' : 'offline';
  el.conn.textContent = online ? 'connected' : 'offline';
}

/** Grow the textarea with its content, up to the CSS max-height. */
export function autosize() {
  el.input.style.height = 'auto';
  el.input.style.height = `${el.input.scrollHeight}px`;
}
