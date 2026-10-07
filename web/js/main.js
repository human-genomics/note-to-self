// Boot: fonts, panes, initial chat, polling for external edits, timers, shortcuts.

import { api, onLocked } from './api.js';
import { state, on, chatByName, syncDrafts, migrateLocalDrafts, flushDrafts } from './store.js';
import { openSettings, showLocked } from './settings.js';
import { bubbleTime, listTime, separatorText } from './format.js';
import * as leftpane from './leftpane.js';
import * as conversation from './conversation.js';
import * as timeline from './timeline.js';
import * as composer from './composer.js';
import { hasPending } from './sendq.js';
import { dialogIsOpen, menuIsOpen, pickerIsOpen } from './overlays.js';

const POLL_MS = 2000;
let failures = 0;
let missingPolls = 0;
let polling = false;

async function refreshChats() {
  const res = await api.chats();
  state.chats = res.chats;
  syncDrafts(res.chats);
  leftpane.renderChats();
  return res.chats;
}

async function poll() {
  if (polling || state.halted || document.visibilityState !== 'visible') return;
  polling = true;
  try {
    const chats = await refreshChats();
    failures = 0;
    leftpane.setOnline(true);
    const cur = chats.find((c) => c.name === state.current);
    if (cur) {
      missingPolls = 0;
      if (timeline.currentChat() === cur.name) timeline.checkRev(cur.rev);
    } else if (state.current && state.chatOps === 0 && (missingPolls += 1) >= 2) {
      // The open chat was renamed or removed outside the app (seen on two polls in a row).
      missingPolls = 0;
      state.current = null;
      conversation.openChat(state.defaultChat);
    }
  } catch {
    failures += 1;
    if (failures >= 2) leftpane.setOnline(false);
  } finally {
    polling = false;
  }
}

// Re-render every relative time label ("Now", "5m", "Yesterday", ...).
function tick() {
  for (const t of document.querySelectorAll('time[data-fmt]')) {
    const { fmt } = t.dataset;
    let text;
    if (fmt === 'bubble') text = bubbleTime(Number(t.dataset.ts));
    else if (fmt === 'list') text = listTime(Number(t.dataset.ts));
    else if (fmt === 'sep') text = separatorText(t.dataset.date);
    if (text && t.textContent !== text) t.textContent = text;
  }
}

function onKeyDown(e) {
  const mod = e.metaKey || e.ctrlKey;
  if (state.halted) return;
  if (mod && !e.altKey && !e.shiftKey && e.key === ',') {
    e.preventDefault();
    if (!dialogIsOpen()) openSettings();
    return;
  }
  if (mod && !e.altKey && e.key.toLowerCase() === 'f') {
    e.preventDefault();
    leftpane.startSearch(e.shiftKey ? 'chat' : 'all');
    return;
  }
  if (e.key === 'Escape' && !dialogIsOpen() && !menuIsOpen() && !pickerIsOpen()) {
    if (timeline.isSelecting()) {
      e.preventDefault();
      timeline.exitSelect();
      return;
    }
  }
  composer.handleStrayKey(e);
}

async function boot() {
  const params = new URLSearchParams(location.search);
  if (params.get('strip') === '1') document.documentElement.style.setProperty('--strip', '28px');
  const os = (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || '';
  document.documentElement.dataset.os = /mac/i.test(os) ? 'mac' : 'other';

  try {
    await Promise.all([
      document.fonts.load('400 14px Inter'),
      document.fonts.load('500 14px Inter'),
      document.fonts.load('600 14px Inter'),
    ]);
  } catch {
    /* fall back to system fonts */
  }

  leftpane.mount(document.getElementById('left'));
  conversation.mount(document.getElementById('conv'), { onSearch: () => leftpane.startSearch('chat') });
  onLocked(showLocked);
  document.addEventListener('keydown', onKeyDown); // shortcuts work while the first chat loads

  try {
    const info = await api.ping();
    state.version = info.version || '';
    if (!info.root) {
      showLocked(); // this browser hasn't been given the key
      return;
    }
    state.root = info.root;
    state.rootDisplay = info.rootDisplay;
    state.defaultChat = info.defaultChat || state.defaultChat;
    state.platform = info.platform || '';
  } catch {
    leftpane.setOnline(false);
  }
  try {
    await refreshChats();
    migrateLocalDrafts();
  } catch {
    leftpane.setOnline(false);
  }
  if (state.halted) return;

  let initial = state.defaultChat;
  try {
    const fromHash = decodeURIComponent(location.hash.slice(1));
    if (fromHash && chatByName(fromHash)) initial = fromHash;
  } catch {
    /* ignore */
  }
  await conversation.openChat(initial);
  composer.focus();

  on('chats-dirty', () => refreshChats().catch(() => {}));
  on('sent', () => refreshChats().catch(() => {}));

  setInterval(poll, POLL_MS);
  setInterval(tick, 60_000);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      poll();
      tick();
    } else {
      flushDrafts(true);
    }
  });
  window.addEventListener('pagehide', () => flushDrafts(true));
  window.addEventListener('focus', () => {
    poll();
    tick();
  });
  window.addEventListener('hashchange', () => {
    try {
      const name = decodeURIComponent(location.hash.slice(1));
      if (name && name !== state.current && chatByName(name)) conversation.openChat(name);
    } catch {
      /* ignore malformed hashes */
    }
  });
  window.addEventListener('beforeunload', (e) => {
    flushDrafts(true);
    if (state.halted) return;
    if (hasPending() || composer.hasUnsavedState()) {
      e.preventDefault();
      e.returnValue = '';
    }
  });
}

boot();
