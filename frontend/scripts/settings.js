/**
 * UI preferences, stored in localStorage. Nothing here touches backend config —
 * the model and base_url come from env / config.json at the repo root and are
 * read once at process start, so they are not the frontend's to change.
 *
 * This module must not import ui.js: ui.js reads preferences from here, and a
 * cycle between the two would be easy to introduce by accident.
 */

import { CONFIG } from './config.js';
import * as api from './api.js';
import * as background from './background.js';

const DEFAULTS = {
  theme: 'system',       // system | light | dark
  toolsExpanded: false,  // whether tool blocks start open
  backgroundOpacity: 50, // how much of the log background image shows through
  sidebarCollapsed: false, // docked history sidebar retracted (wide screens)
};

const el = {
  dialog: document.getElementById('settings'),
  open: document.getElementById('settings-btn'),
  close: document.getElementById('settings-close'),
  themeRow: document.getElementById('theme-row'),
  toolsToggle: document.getElementById('tools-expanded'),
  dropzone: document.getElementById('dropzone'),
  dropzoneText: document.getElementById('dropzone-text'),
  bgFile: document.getElementById('bg-file'),
  bgControls: document.getElementById('bg-controls'),
  bgOpacity: document.getElementById('bg-opacity'),
  bgOpacityOut: document.getElementById('bg-opacity-out'),
  bgRemove: document.getElementById('bg-remove'),
  bgStatus: document.getElementById('bg-status'),
  dangerCount: document.getElementById('danger-count'),
  dangerInput: document.getElementById('danger-input'),
  dangerBtn: document.getElementById('danger-btn'),
  dangerResult: document.getElementById('danger-result'),
};

const CONFIRM_WORD = 'DELETE';

let current = { ...DEFAULTS };
let onSessionsDestroyed = null;

// ── Storage ───────────────────────────────────────────────────────────

function load() {
  try {
    const saved = JSON.parse(localStorage.getItem(CONFIG.SETTINGS_KEY));
    return { ...DEFAULTS, ...(saved || {}) };
  } catch {
    return { ...DEFAULTS };
  }
}

function persist() {
  localStorage.setItem(CONFIG.SETTINGS_KEY, JSON.stringify(current));
}

/** Read one preference. */
export function get(key) {
  return current[key];
}

/** Write one preference and apply it. Exported for state owned by other modules. */
export function set(key, value) {
  current[key] = value;
  persist();
  apply();
}

// ── Applying ──────────────────────────────────────────────────────────

/**
 * Push the current preferences into the document.
 *
 * The theme is expressed as a data-theme attribute on <html>; 'system' removes
 * it so the prefers-color-scheme media query takes over again. index.html sets
 * this attribute inline before first paint to avoid a flash of the wrong theme,
 * so keep the two in agreement.
 */
export function apply() {
  const root = document.documentElement;

  if (current.theme === 'system') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', current.theme);

  // Existing tool blocks follow the new default, except ones the user has
  // already toggled by hand this session.
  for (const details of document.querySelectorAll('.tool:not([data-touched])')) {
    details.open = current.toolsExpanded;
  }

  background.setOpacity(current.backgroundOpacity);

  // Only has an effect above the drawer breakpoint; see main.css.
  document.body.classList.toggle('sidebar-collapsed', current.sidebarCollapsed);

  syncControls();
}

function syncControls() {
  for (const btn of el.themeRow.querySelectorAll('[data-theme-value]')) {
    const selected = btn.dataset.themeValue === current.theme;
    btn.classList.toggle('active', selected);
    btn.setAttribute('aria-pressed', String(selected));
  }
  el.toolsToggle.checked = current.toolsExpanded;
}

// ── Background image ──────────────────────────────────────────────────

let hasBackground = false;

function syncBackgroundControls() {
  el.bgControls.hidden = !hasBackground;
  el.dropzoneText.textContent = hasBackground
    ? 'Drop a new image to replace'
    : 'Drop an image here';
  el.bgOpacity.value = String(current.backgroundOpacity);
  el.bgOpacityOut.textContent = `${current.backgroundOpacity}%`;
}

async function acceptFile(file) {
  el.bgStatus.textContent = 'Loading…';

  const result = await background.set(file);
  if (!result.ok) {
    el.bgStatus.textContent = result.error;
    return;
  }

  hasBackground = true;
  el.bgStatus.textContent = '';
  syncBackgroundControls();
}

async function removeBackground() {
  await background.clear();
  hasBackground = false;
  el.bgStatus.textContent = '';
  syncBackgroundControls();
}

function wireDropzone() {
  // Click / keyboard both reach the file picker, since drag-and-drop alone is
  // unusable without a pointer.
  el.dropzone.addEventListener('click', () => el.bgFile.click());

  el.bgFile.addEventListener('change', () => {
    const file = el.bgFile.files?.[0];
    if (file) acceptFile(file);
    el.bgFile.value = ''; // re-selecting the same file should still fire change
  });

  // Both dragover and dragenter must be cancelled or the browser navigates to
  // the dropped file instead of handing it over.
  for (const type of ['dragenter', 'dragover']) {
    el.dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      el.dropzone.classList.add('is-over');
    });
  }

  for (const type of ['dragleave', 'dragend']) {
    el.dropzone.addEventListener(type, () => el.dropzone.classList.remove('is-over'));
  }

  el.dropzone.addEventListener('drop', (event) => {
    event.preventDefault();
    el.dropzone.classList.remove('is-over');

    const file = event.dataTransfer?.files?.[0];
    if (file) acceptFile(file);
    else el.bgStatus.textContent = 'That drop contained no file.';
  });

  el.bgOpacity.addEventListener('input', () => {
    // Live preview while dragging; only the committed value is persisted.
    el.bgOpacityOut.textContent = `${el.bgOpacity.value}%`;
    background.setOpacity(el.bgOpacity.value);
  });

  el.bgOpacity.addEventListener('change', () => set('backgroundOpacity', Number(el.bgOpacity.value)));

  el.bgRemove.addEventListener('click', removeBackground);
}

/**
 * A file dropped outside the dropzone would otherwise navigate the whole page
 * to it, silently losing the conversation on screen.
 */
function blockStrayDrops() {
  for (const type of ['dragover', 'drop']) {
    window.addEventListener(type, (event) => {
      if (!el.dropzone.contains(event.target)) event.preventDefault();
    });
  }
}

// ── Danger zone ───────────────────────────────────────────────────────

async function refreshDangerCount() {
  el.dangerResult.textContent = '';

  try {
    const sessions = await api.fetchSessions();
    const n = sessions.length;
    el.dangerCount.textContent =
      n === 1 ? 'Deletes the 1 stored conversation.' : `Deletes all ${n} stored conversations.`;
  } catch {
    el.dangerCount.textContent = 'Deletes every stored conversation.';
  }
}

function validateDangerInput() {
  el.dangerBtn.disabled = el.dangerInput.value.trim().toUpperCase() !== CONFIRM_WORD;
}

async function destroyAll() {
  el.dangerBtn.disabled = true;
  el.dangerResult.textContent = 'Deleting…';

  try {
    const { destroyed } = await api.destroyAllSessions();
    el.dangerResult.textContent = `Deleted ${destroyed} conversation${destroyed === 1 ? '' : 's'}.`;
    el.dangerInput.value = '';
    await onSessionsDestroyed?.();
    refreshDangerCount();
  } catch (err) {
    console.error('Could not delete all sessions:', err);
    el.dangerResult.textContent = `Failed: ${err.message}`;
    validateDangerInput();
  }
}

// ── Dialog ────────────────────────────────────────────────────────────

function openDialog() {
  el.dangerInput.value = '';
  el.bgStatus.textContent = '';
  validateDangerInput();
  refreshDangerCount();
  syncControls();
  syncBackgroundControls();
  el.dialog.showModal();
}

// ── Init ──────────────────────────────────────────────────────────────

/**
 * @param {object} handlers
 * @param {() => Promise<void>} handlers.onSessionsDestroyed  reset app state
 */
export function init(handlers = {}) {
  onSessionsDestroyed = handlers.onSessionsDestroyed;
  current = load();

  el.open.addEventListener('click', openDialog);
  el.close.addEventListener('click', () => el.dialog.close());

  el.themeRow.addEventListener('click', (event) => {
    const btn = event.target.closest('[data-theme-value]');
    if (btn) set('theme', btn.dataset.themeValue);
  });

  el.toolsToggle.addEventListener('change', () => {
    set('toolsExpanded', el.toolsToggle.checked);
  });

  el.dangerInput.addEventListener('input', validateDangerInput);
  el.dangerBtn.addEventListener('click', destroyAll);

  wireDropzone();
  blockStrayDrops();

  // IndexedDB is async, so the stored image lands a tick after first paint.
  background.restore().then((found) => {
    hasBackground = found;
    syncBackgroundControls();
  });

  // Clicking the backdrop closes the dialog; clicks inside it must not.
  el.dialog.addEventListener('click', (event) => {
    if (event.target === el.dialog) el.dialog.close();
  });

  apply();
}
