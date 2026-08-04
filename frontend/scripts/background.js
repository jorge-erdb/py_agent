/**
 * Custom log background image.
 *
 * The image lives in IndexedDB as a Blob rather than in localStorage: photos
 * here run to several megabytes, localStorage tops out around 5MB and stores
 * strings as UTF-16, so a base64'd 4MB image reliably throws QuotaExceededError.
 * IndexedDB stores the binary as-is.
 *
 * Only the display settings (opacity) live in localStorage alongside the other
 * preferences — see settings.js.
 */

const DB_NAME = 'agent-console';
const DB_VERSION = 1;
const STORE = 'assets';
const KEY = 'log-background';

/** Refuse anything larger than this; well past any sensible wallpaper. */
export const MAX_BYTES = 20 * 1024 * 1024;

// The object URL currently applied to the document, so it can be revoked when
// replaced. Leaking these keeps whole images alive in memory.
let activeUrl = null;

// ── IndexedDB ─────────────────────────────────────────────────────────

function openDB() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(STORE)) {
        request.result.createObjectStore(STORE);
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function transact(mode, run) {
  return openDB().then(
    (db) =>
      new Promise((resolve, reject) => {
        const tx = db.transaction(STORE, mode);
        const request = run(tx.objectStore(STORE));
        tx.oncomplete = () => {
          db.close();
          resolve(request?.result);
        };
        tx.onerror = () => {
          db.close();
          reject(tx.error);
        };
      })
  );
}

function save(blob) {
  return transact('readwrite', (store) => store.put(blob, KEY));
}

function read() {
  return transact('readonly', (store) => store.get(KEY));
}

function remove() {
  return transact('readwrite', (store) => store.delete(KEY));
}

// ── Applying ──────────────────────────────────────────────────────────

/**
 * Point the document at a blob, or clear the background when given null.
 *
 * The image is exposed as a CSS custom property and paired with a `has-bg`
 * class so the stylesheet can skip the veil layer entirely when unset.
 */
function surface() {
  // The image sits on .main rather than .log so it also shows through the
  // transparent composer, letting the input form read as floating over it.
  // The topbar covers its own strip with an opaque background.
  return document.querySelector('.main');
}

function paint(blob) {
  const target = surface();

  if (activeUrl) {
    URL.revokeObjectURL(activeUrl);
    activeUrl = null;
  }

  if (!blob) {
    target.classList.remove('has-bg');
    target.style.removeProperty('--chat-bg');
    return;
  }

  activeUrl = URL.createObjectURL(blob);
  target.style.setProperty('--chat-bg', `url("${activeUrl}")`);
  target.classList.add('has-bg');
}

/**
 * Set how much of the image shows through, 0-100.
 *
 * Implemented as a veil of the theme's own background colour painted over the
 * image, because `background-image` cannot be given an opacity directly. The
 * veil uses --bg-rgb so it tracks light/dark automatically.
 */
export function setOpacity(percent) {
  const clamped = Math.min(100, Math.max(0, Number(percent) || 0));
  surface().style.setProperty('--chat-veil', String(1 - clamped / 100));
}

// ── Public API ────────────────────────────────────────────────────────

/** Load and apply the stored background, if any. Safe to call when none exists. */
export async function restore() {
  try {
    const blob = await read();
    paint(blob || null);
    return !!blob;
  } catch (err) {
    console.error('Could not load background:', err);
    return false;
  }
}

/**
 * Validate, store and apply a dropped file.
 * @returns {Promise<{ok: true} | {ok: false, error: string}>}
 */
export async function set(file) {
  if (!file) return { ok: false, error: 'No file received.' };

  if (!file.type.startsWith('image/')) {
    return { ok: false, error: `Not an image (${file.type || 'unknown type'}).` };
  }

  if (file.size > MAX_BYTES) {
    const mb = (file.size / 1024 / 1024).toFixed(1);
    return { ok: false, error: `Too large — ${mb}MB, limit is ${MAX_BYTES / 1024 / 1024}MB.` };
  }

  try {
    await save(file);
    paint(file);
    return { ok: true };
  } catch (err) {
    console.error('Could not store background:', err);
    return { ok: false, error: err.name === 'QuotaExceededError'
      ? 'Not enough browser storage for this image.'
      : `Could not save: ${err.message}` };
  }
}

/** Remove the stored background. */
export async function clear() {
  try {
    await remove();
    paint(null);
    return true;
  } catch (err) {
    console.error('Could not remove background:', err);
    return false;
  }
}
