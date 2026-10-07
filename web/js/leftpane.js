// Left pane: header, search (all chats or "this chat"), chat list, resizer, offline banner.

import { h, icon, avatar, badge } from './dom.js';
import { api } from './api.js';
import { state, on, getDraft, isDefaultChat, saveLocal, loadLocal } from './store.js';
import { listTime } from './format.js';
import { renderText, renderSnippet } from './text.js';
import { openMenu, justClosed, toast } from './overlays.js';
import { openChat, chatMenu, newChat } from './conversation.js';
import { openSettings, quitApp } from './settings.js';

let pane;
let listEl;
let searchBox;
let searchInput;
let chipEl;
let clearBtn;
let banner;
let moreBtn;
const rows = new Map(); // chat name -> {sig, el}
let search = null; // {scope: 'all'|'chat', chat, q, timer, seq}

export function mount(el) {
  pane = el;
  const showFolder = () => api.reveal().catch((e) => toast(e.kind === 'http' ? e.message : "Couldn't open the folder"));
  moreBtn = h('button', { class: 'ib', title: 'More', 'aria-label': 'More options' }, icon('more'));
  moreBtn.addEventListener('click', () => {
    if (justClosed(moreBtn)) return;
    openMenu(
      [
        { label: 'New chat', icon: 'compose', action: newChat },
        { label: 'Show notes folder', icon: 'folder', action: showFolder },
        { label: 'Settings', icon: 'settings', action: openSettings },
      ],
      { anchor: moreBtn, align: 'end' },
    );
  });
  const menuBtn = h('button', { class: 'ib', title: 'Menu', 'aria-label': 'Menu' }, icon('menu'));
  menuBtn.addEventListener('click', () => {
    if (justClosed(menuBtn)) return;
    openMenu(
      [
        { label: 'Settings', icon: 'settings', action: openSettings },
        { label: 'Show notes folder', icon: 'folder', action: showFolder },
        { label: 'New chat', icon: 'compose', action: newChat },
        '-',
        { label: 'Quit Note to Self', icon: 'x', action: quitApp },
      ],
      { anchor: menuBtn, align: 'start' },
    );
  });
  const head = h(
    'header',
    { class: 'lp-head' },
    h('div', { class: 'lp-nav' }, menuBtn),
    h('h1', { class: 'lp-title' }, 'Chats'),
    h(
      'div',
      { class: 'lp-actions' },
      h('button', { class: 'ib', title: 'New chat', 'aria-label': 'New chat', onclick: newChat }, icon('compose')),
      moreBtn,
    ),
  );

  searchInput = h('input', { name: 'search', type: 'text', autocomplete: 'off', placeholder: 'Search', spellcheck: 'false', 'aria-label': 'Search' });
  chipEl = h('span', { class: 'search-chip', hidden: true });
  clearBtn = h('button', { class: 'ib search-clear', title: 'Clear search', 'aria-label': 'Clear search', hidden: true, onclick: () => endSearch(true) }, icon('x'));
  searchBox = h('div', { class: 'search-box' }, icon('search16'), chipEl, searchInput, clearBtn);
  const searchRow = h(
    'div',
    { class: 'lp-search' },
    searchBox,
    h('button', { class: 'ib lp-filter', tabindex: '-1', 'aria-hidden': 'true' }, icon('filter')),
  );

  banner = h(
    'div',
    { class: 'banner', role: 'alert', hidden: true },
    icon('error'),
    h('div', null, "Can't reach Note to Self. Is ", h('code', null, './notetoself'), ' running?'),
  );
  listEl = h('div', { class: 'lp-list', role: 'list' });
  const resizer = h('div', { class: 'resizer', title: 'Drag to resize' });
  pane.append(head, searchRow, banner, listEl, resizer);

  searchInput.addEventListener('input', onSearchInput);
  searchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      endSearch(true);
    } else if (e.key === 'Enter') {
      const first = listEl.querySelector('.chat-row');
      if (first) first.click();
    } else if (e.key === 'Backspace' && !searchInput.value && search && search.scope === 'chat') {
      setScope('all');
    }
  });
  searchInput.addEventListener('blur', () => {
    if (search && !searchInput.value.trim()) setTimeout(() => document.activeElement !== searchInput && endSearch(false), 150);
  });
  setupResizer(resizer);

  on('chat-opened', () => renderChats());
  on('draft', () => renderChats());
}

/* ---------- Chat list ---------- */

function attachmentLabel(a) {
  if (a.kind === 'image') return '📷 Photo';
  if (a.kind === 'video') return '🎥 Video';
  if (a.kind === 'audio') return '🎤 Audio';
  return `📎 ${a.name}`;
}

function previewOf(c, draft) {
  if (draft) {
    const frag = document.createDocumentFragment();
    frag.append(h('span', { class: 'draft' }, 'Draft:'), renderText(draft.text, { links: false }));
    return frag;
  }
  if (!c.last) return '';
  const atts = c.last.attachments || [];
  if (c.last.text) {
    const prefix = atts.length ? `${attachmentLabel(atts[0]).split(' ')[0]} ` : '';
    return renderText(prefix + c.last.text, { links: false });
  }
  return atts.length ? renderText(attachmentLabel(atts[0]), { links: false }) : '';
}

function chatRow(c) {
  const d = getDraft(c.name);
  const draft = d && (!c.lastTs || d.ts > c.lastTs) ? d : null;
  const ts = draft ? draft.ts : c.lastTs;
  const el = h(
    'button',
    {
      class: c.name === state.current ? 'chat-row sel' : 'chat-row',
      role: 'listitem',
      dataset: { name: c.name },
      onclick: () => openChat(c.name),
      oncontextmenu: (e) => {
        e.preventDefault();
        chatMenu(c.name, { x: e.clientX, y: e.clientY });
      },
    },
    avatar(c.name, c.isDefault, 48),
    h(
      'div',
      { class: 'chat-body' },
      h(
        'div',
        { class: 'chat-top' },
        h('div', { class: 'chat-name' }, c.name, c.isDefault ? badge() : null),
        ts ? h('time', { class: 'chat-time', dataset: { ts: String(ts), fmt: 'list' } }, listTime(ts)) : null,
      ),
      h(
        'div',
        { class: 'chat-bottom' },
        h('div', { class: 'chat-preview' }, previewOf(c, draft)),
        c.last && !draft ? icon('read', 'chat-status') : null,
      ),
    ),
  );
  return { el, sig: JSON.stringify([c.name, c.isDefault, c.lastTs, c.last, draft && draft.text, c.name === state.current]) };
}

export function renderChats({ force = false } = {}) {
  if (search && !(force || (search.scope === 'all' && !search.q))) return;
  const seen = new Set();
  // Drop anything that isn't a chat row (e.g. leftover search results).
  for (const child of [...listEl.children]) if (!child.dataset.name || child.dataset.result) child.remove();
  let ref = listEl.firstChild;
  for (const c of state.chats) {
    const next = chatRow(c);
    const prev = rows.get(c.name);
    let el;
    if (prev && prev.sig === next.sig && prev.el.isConnected) el = prev.el;
    else {
      el = next.el;
      if (prev && prev.el.isConnected) {
        if (prev.el === ref) ref = el;
        prev.el.replaceWith(el);
      }
      rows.set(c.name, next);
    }
    seen.add(c.name);
    if (el === ref) ref = ref.nextSibling;
    else listEl.insertBefore(el, ref);
  }
  for (const [name, r] of rows) {
    if (!seen.has(name)) {
      r.el.remove();
      rows.delete(name);
    }
  }
}

export function setOnline(online) {
  if (state.online === online) return;
  state.online = online;
  banner.hidden = online;
}

/* ---------- Search ---------- */

export function startSearch(scope) {
  if (scope === 'chat' && !state.current) scope = 'all';
  search = search || { q: '', seq: 0, timer: 0 };
  setScope(scope);
  searchInput.focus();
  searchInput.select();
}

function setScope(scope) {
  search.scope = scope;
  search.chat = scope === 'chat' ? state.current : null;
  searchBox.classList.toggle('scoped', scope === 'chat');
  chipEl.hidden = scope !== 'chat';
  if (scope === 'chat') {
    chipEl.replaceChildren(
      avatar(search.chat, isDefaultChat(search.chat), 20),
      h('button', { class: 'ib', title: 'Search all chats', 'aria-label': 'Search all chats', onclick: () => setScope('all') }, icon('x')),
    );
  }
  searchInput.placeholder = scope === 'chat' ? 'Search chat' : 'Search';
  runSearch();
}

function onSearchInput() {
  if (!search) search = { scope: 'all', chat: null, q: '', seq: 0, timer: 0 };
  clearBtn.hidden = !searchInput.value;
  clearTimeout(search.timer);
  search.timer = setTimeout(runSearch, 200);
}

async function runSearch() {
  if (!search) return;
  const q = searchInput.value.trim();
  search.q = q;
  clearBtn.hidden = !searchInput.value;
  if (!q) {
    showResults(null);
    return;
  }
  const seq = ++search.seq;
  try {
    const res = await api.search(q, search.chat || undefined);
    if (!search || seq !== search.seq) return;
    showResults(res);
  } catch {
    if (search && seq === search.seq) showResults({ chats: [], messages: [], error: true });
  }
}

function resultRow(r) {
  const isDefault = isDefaultChat(r.chat);
  return h(
    'button',
    {
      class: 'chat-row',
      role: 'listitem',
      dataset: { name: r.chat, result: '1' },
      onclick: () => openChat(r.chat, { jumpTo: { id: r.id, date: r.date } }),
    },
    avatar(r.chat, isDefault, 48),
    h(
      'div',
      { class: 'chat-body' },
      h(
        'div',
        { class: 'chat-top' },
        h('div', { class: 'chat-name' }, r.chat, isDefault ? badge() : null),
        h('time', { class: 'chat-time', dataset: { ts: String(r.ts), fmt: 'list' } }, listTime(r.ts)),
      ),
      h('div', { class: 'chat-bottom' }, h('div', { class: 'chat-preview' }, renderSnippet(r.snippet))),
    ),
  );
}

function showResults(res) {
  listEl.replaceChildren();
  rows.clear();
  if (!res) {
    // Empty query: keep showing the normal chat list under the search box.
    if (search && search.scope === 'all') renderChats({ force: true });
    return;
  }
  const out = [];
  if (res.error) {
    out.push(h('div', { class: 'lp-empty' }, "Can't reach the Note to Self server."));
  } else {
    if (search.scope === 'all' && res.chats.length) {
      out.push(h('div', { class: 'lp-section' }, 'Chats'));
      for (const c of res.chats) {
        const full = state.chats.find((x) => x.name === c.name) || { ...c, last: null, lastTs: null };
        const row = chatRow(full).el;
        row.dataset.result = '1';
        out.push(row);
      }
    }
    if (res.messages.length) {
      out.push(h('div', { class: 'lp-section' }, 'Messages'));
      for (const r of res.messages) out.push(resultRow(r));
    }
    if (!out.length) {
      out.push(
        h(
          'div',
          { class: 'lp-empty' },
          search.scope === 'chat' ? `No results for "${search.q}" in ${search.chat}` : `No results for "${search.q}"`,
        ),
      );
    }
  }
  listEl.append(...out);
  listEl.scrollTop = 0;
}

export function endSearch(clearInput) {
  if (!search) return;
  clearTimeout(search.timer);
  search = null;
  if (clearInput) searchInput.value = '';
  searchInput.placeholder = 'Search';
  searchBox.classList.remove('scoped');
  chipEl.hidden = true;
  clearBtn.hidden = !searchInput.value;
  listEl.replaceChildren();
  rows.clear();
  renderChats();
  if (clearInput) searchInput.blur();
}

export function searchActive() {
  return !!search;
}

/* ---------- Resizer ---------- */

export function applyPaneWidth(w) {
  const width = Math.max(280, Math.min(380, Math.round(w)));
  document.documentElement.style.setProperty('--pane-w', `${width}px`);
  return width;
}

function setupResizer(handle) {
  applyPaneWidth(loadLocal('paneW', 320));
  handle.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    handle.setPointerCapture(e.pointerId);
    const startX = e.clientX;
    const startW = pane.getBoundingClientRect().width;
    let w = startW;
    const move = (ev) => {
      w = applyPaneWidth(startW + ev.clientX - startX);
    };
    const up = () => {
      handle.removeEventListener('pointermove', move);
      handle.removeEventListener('pointerup', up);
      handle.removeEventListener('pointercancel', up);
      saveLocal('paneW', w);
    };
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', up);
    handle.addEventListener('pointercancel', up);
  });
}
