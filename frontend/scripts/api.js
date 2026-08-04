/**
 * Backend communication. Every fetch to the API lives here.
 */

import { CONFIG } from './config.js';
import { readNDJSON } from './stream.js';

let controller = null;

/** True while a chat request is in flight. */
export function isStreaming() {
  return controller !== null;
}

/**
 * Send a turn and yield each streamed message as it arrives.
 *
 * The generator ends when the backend emits its {_final: true} sentinel or the
 * stream closes; the caller decides what to do with each message.
 *
 * @param {string} text        user input
 * @param {string|null} sessionId
 */
export async function* sendMessage(text, sessionId) {
  // One turn at a time — abandon anything still running.
  abort();
  controller = new AbortController();

  try {
    const response = await fetch(CONFIG.ENDPOINTS.CHAT_STREAM, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messages: [{ role: 'user', content: text }],
        session_id: sessionId,
      }),
      signal: controller.signal,
    });

    if (!response.ok) {
      throw new Error(`Server responded ${response.status}`);
    }

    yield* readNDJSON(response);
  } finally {
    controller = null;
  }
}

/** Abort the in-flight chat request, if any. Returns whether one was cancelled. */
export function abort() {
  if (!controller) return false;
  controller.abort();
  controller = null;
  return true;
}

/** Create a fresh session. Returns the new session id. */
export async function createSession() {
  const response = await fetch(CONFIG.ENDPOINTS.NEW_SESSION, { method: 'POST' });
  if (!response.ok) throw new Error(`Server responded ${response.status}`);
  const data = await response.json();
  return data.session_id;
}

/** Fetch a session's persisted messages. */
export async function fetchSessionMessages(sessionId) {
  const url = `${CONFIG.ENDPOINTS.SESSIONS}/${encodeURIComponent(sessionId)}/messages`;
  const response = await fetch(url);

  // A stale id in localStorage is expected after the DB is wiped — the caller
  // handles this by starting fresh rather than surfacing an error.
  if (response.status === 404) return null;
  if (!response.ok) throw new Error(`Server responded ${response.status}`);

  const data = await response.json();
  return data.messages || [];
}

/** List all sessions, newest first. Each is {id, created_at, last_active}. */
export async function fetchSessions() {
  const response = await fetch(CONFIG.ENDPOINTS.SESSIONS, { cache: 'no-store' });
  if (!response.ok) throw new Error(`Server responded ${response.status}`);
  return response.json();
}

/**
 * Destroy a session permanently.
 *
 * Note the verb: the backend exposes this as POST /sessions/{id}, not DELETE.
 */
export async function destroySession(sessionId) {
  const url = `${CONFIG.ENDPOINTS.SESSIONS}/${encodeURIComponent(sessionId)}`;
  const response = await fetch(url, { method: 'POST' });
  if (!response.ok) throw new Error(`Server responded ${response.status}`);
  return response.json();
}

/** Destroy every session. Returns {destroyed: n}. Irreversible. */
export async function destroyAllSessions() {
  const response = await fetch(CONFIG.ENDPOINTS.DESTROY_ALL, { method: 'POST' });
  if (!response.ok) throw new Error(`Server responded ${response.status}`);
  return response.json();
}

/** Clear a session's history server-side. */
export async function clearHistory(sessionId) {
  const response = await fetch(CONFIG.ENDPOINTS.CLEAR, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId }),
  });
  if (!response.ok) throw new Error(`Server responded ${response.status}`);
  return response.json();
}

/** Cheap liveness probe for the connection indicator. */
export async function checkHealth() {
  try {
    const response = await fetch(CONFIG.ENDPOINTS.HEALTH, { cache: 'no-store' });
    return response.ok;
  } catch {
    return false;
  }
}

// ── Session id persistence ────────────────────────────────────────────

export function getStoredSessionId() {
  return localStorage.getItem(CONFIG.SESSION_KEY);
}

export function storeSessionId(sessionId) {
  if (sessionId) localStorage.setItem(CONFIG.SESSION_KEY, sessionId);
}

export function clearStoredSessionId() {
  localStorage.removeItem(CONFIG.SESSION_KEY);
}
