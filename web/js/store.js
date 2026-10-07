// App state, a tiny event bus, drafts (saved in the notes folder), and per-browser
// conveniences in localStorage (pane width, recent emoji: nothing you wrote).

import { api } from './api.js';

export const state = {
  root: '',
  rootDisplay: '~/NoteToSelf',
  defaultChat: 'Note to Self',
  version: '',
  platform: '',
  online: true,
  halted: false, // locked out, or all data deleted: stop polling
  chats: [],
  current: null,
  writes: 0, // writes in flight; polling skips resyncs while > 0
  chatOps: 0, // chat create/rename/delete in flight; polling won't react to vanished chats
};

const subs = new Map();

export function on(event, fn) {
  if (!subs.has(event)) subs.set(event, new Set());
  subs.get(event).add(fn);
  return () => subs.get(event).delete(fn);
}

export function emit(event, data) {
  for (const fn of subs.get(event) || []) fn(data);
}

export function chatByName(name) {
  return state.chats.find((c) => c.name === name) || null;
}

export function isDefaultChat(name) {
  const c = chatByName(name);
  return c ? c.isDefault : name === state.defaultChat;
}

// localStorage can throw (private windows, blocked storage); never let that break the app.
export function loadLocal(key, fallback) {
  try {
    const raw = localStorage.getItem(`nts.${key}`);
    return raw == null ? fallback : JSON.parse(raw);
  } catch {
    return fallback;
  }
}

export function saveLocal(key, value) {
  try {
    localStorage.setItem(`nts.${key}`, JSON.stringify(value));
  } catch {
    /* ignore */
  }
}

export function clearLocal() {
  try {
    for (const k of Object.keys(localStorage)) if (k.startsWith('nts.')) localStorage.removeItem(k);
  } catch {
    /* ignore */
  }
}

/* ---------- Drafts ----------
 * Unsent text lives in <chat>/.draft.md, so everything you write stays in the notes folder.
 * Local edits are saved shortly after you stop typing; until then they win over what the
 * server reports. */

const SAVE_MS = 400;
const drafts = new Map(); // chat -> {text, ts}
const unsaved = new Map(); // chat -> {text, timer}
const saving = new Set();

export function getDraft(chat) {
  const d = drafts.get(chat);
  return d && d.text.trim() ? d : null;
}

export function setDraft(chat, text) {
  const known = getDraft(chat);
  if (!unsaved.has(chat) && (known ? known.text : '') === (text.trim() ? text : '')) return;
  if (text.trim()) drafts.set(chat, { text, ts: Date.now() });
  else drafts.delete(chat);
  const job = unsaved.get(chat);
  if (job) clearTimeout(job.timer);
  unsaved.set(chat, { text, timer: setTimeout(() => saveDraft(chat), SAVE_MS) });
}

async function saveDraft(chat, keepalive = false) {
  const job = unsaved.get(chat);
  if (!job) return;
  clearTimeout(job.timer);
  unsaved.delete(chat);
  saving.add(chat);
  try {
    await api.saveDraft(chat, job.text, keepalive);
  } catch (e) {
    // Offline for a moment: try again, unless newer text has replaced it meanwhile.
    if (e.kind === 'offline' && !unsaved.has(chat) && !state.halted) {
      unsaved.set(chat, { text: job.text, timer: setTimeout(() => saveDraft(chat), 3000) });
    }
  } finally {
    saving.delete(chat);
  }
}

// Save every pending draft now (keepalive: the window is closing).
export function flushDrafts(keepalive = false) {
  return Promise.all([...unsaved.keys()].map((chat) => saveDraft(chat, keepalive)));
}

export function hasUnsavedDrafts() {
  return unsaved.size > 0 || saving.size > 0;
}

// Drafts as saved on disk, from /api/chats.
export function syncDrafts(chats) {
  const names = new Set();
  for (const c of chats) {
    names.add(c.name);
    if (unsaved.has(c.name) || saving.has(c.name)) continue;
    if (c.draft && c.draft.text.trim()) drafts.set(c.name, c.draft);
    else drafts.delete(c.name);
  }
  for (const name of [...drafts.keys()]) if (!names.has(name) && !unsaved.has(name)) drafts.delete(name);
}

export function moveDraft(from, to) {
  const d = drafts.get(from);
  if (d) {
    drafts.set(to, d);
    drafts.delete(from);
  }
  const job = unsaved.get(from);
  if (job) {
    clearTimeout(job.timer);
    unsaved.delete(from);
    unsaved.set(to, { text: job.text, timer: setTimeout(() => saveDraft(to), SAVE_MS) });
  }
}

export function dropAllDrafts() {
  for (const job of unsaved.values()) clearTimeout(job.timer);
  unsaved.clear();
  drafts.clear();
}

// Earlier versions kept drafts in browser storage; move them into the notes folder once.
export function migrateLocalDrafts() {
  const old = loadLocal('drafts', null);
  if (!old || typeof old !== 'object') return;
  for (const [chat, d] of Object.entries(old)) {
    if (d && typeof d.text === 'string' && d.text.trim() && !getDraft(chat) && chatByName(chat)) setDraft(chat, d.text);
  }
  try {
    localStorage.removeItem('nts.drafts');
  } catch {
    /* ignore */
  }
}
