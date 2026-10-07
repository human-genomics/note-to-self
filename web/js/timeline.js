// The conversation timeline: loads whole days, renders keyed rows, keeps the
// scroll position stable, pages older/newer history, select mode and message actions.

import { api } from './api.js';
import { state, emit, on, isDefaultChat } from './store.js';
import { h, icon, avatar, badge } from './dom.js';
import { buildRow, disposeRow, setSpacersChangedHandler } from './message.js';
import { separatorText, sameGroup, fullTime, formatSize } from './format.js';
import { openMenu, justClosed, confirmDialog, toast, infoDialog, lightbox } from './overlays.js';
import { pendingFor, retry as retrySend, discard as discardPending, jobById } from './sendq.js';

const PAGE = 100;
const FAR_FUTURE = '9999-12-31';

const tl = {
  chat: null,
  msgs: [],
  hasBefore: false,
  hasAfter: false,
  rev: null,
  loading: null,
  pinned: true,
  gen: 0,
  selecting: false,
  sel: new Set(),
  selAnchor: null,
};

let wrap;
let scroller;
let list;
let topSentinel;
let bottomSentinel;
let pill;
let downBtn;
const nodes = new Map();
let ignoreScrollUntil = 0;
let pillTimer = 0;

/* ---------- Mount ---------- */

export function mount(parent) {
  topSentinel = h('div', { class: 'tl-sentinel' });
  bottomSentinel = h('div', { class: 'tl-sentinel' });
  list = h('div', { class: 'tl-list' }, topSentinel, bottomSentinel);
  scroller = h('div', { class: 'tl', tabindex: '-1' }, list);
  pill = h('div', { class: 'date-pill', 'aria-hidden': 'true' });
  downBtn = h(
    'button',
    { class: 'scroll-down', title: 'Scroll to bottom of chat', 'aria-label': 'Scroll to bottom of chat', hidden: true, onclick: goLatest },
    icon('chevron-down'),
  );
  wrap = h('div', { class: 'tl-wrap' }, scroller, pill, downBtn);
  parent.append(wrap);

  scroller.addEventListener('scroll', onScroll, { passive: true });
  scroller.addEventListener('click', onClick);
  scroller.addEventListener('contextmenu', onContextMenu);

  // Stay pinned to the bottom while the viewport changes size, time labels re-flow bubbles,
  // or media finish loading (render() handles everything else).
  const keepPinned = () => {
    if (tl.pinned) scrollToBottom();
  };
  new ResizeObserver(keepPinned).observe(scroller);
  setSpacersChangedHandler(keepPinned);
  scroller.addEventListener('load', keepPinned, true);
  scroller.addEventListener('loadedmetadata', keepPinned, true);

  on('pending', (chat) => chat === tl.chat && render());
  on('sent', onSent);
  return wrap;
}

/* ---------- Loading ---------- */

function flatten(days) {
  const out = [];
  for (const d of days || []) for (const m of d.messages) out.push(m);
  return out;
}

const oldest = () => (tl.msgs.length ? tl.msgs[0].date : null);
const newest = () => (tl.msgs.length ? tl.msgs[tl.msgs.length - 1].date : null);

function clearNodes() {
  for (const el of nodes.values()) {
    disposeRow(el);
    el.remove();
  }
  nodes.clear();
}

export async function load(chat, { around, highlight } = {}) {
  const gen = ++tl.gen;
  if (tl.chat !== chat) exitSelect();
  tl.chat = chat;
  tl.msgs = [];
  tl.hasBefore = false;
  tl.hasAfter = false;
  tl.rev = null;
  tl.loading = 'initial';
  tl.pinned = !around;
  clearNodes();
  scroller.hidden = true;
  try {
    const res = await api.messages(chat, around ? { around, limit: PAGE } : { limit: PAGE });
    if (gen !== tl.gen) return;
    tl.msgs = flatten(res.days);
    tl.hasBefore = res.hasBefore;
    tl.hasAfter = res.hasAfter;
    tl.rev = res.rev;
  } catch (e) {
    if (gen !== tl.gen) return;
    toast("Couldn't load notes. Is the Note to Self server running?");
  } finally {
    if (gen === tl.gen) tl.loading = null;
  }
  scroller.hidden = false;
  render();
  if (highlight) reveal(highlight);
  else scrollToBottom();
  maybeLoadMore();
}

async function loadOlder() {
  const before = oldest();
  if (!before || tl.loading) return;
  const gen = tl.gen;
  tl.loading = 'older';
  try {
    const res = await api.messages(tl.chat, { before, limit: PAGE });
    if (gen !== tl.gen) return;
    tl.msgs = flatten(res.days).concat(tl.msgs);
    tl.hasBefore = res.hasBefore;
    render();
  } catch {
    /* polling will surface connection problems */
  } finally {
    if (gen === tl.gen) tl.loading = null;
  }
  if (gen === tl.gen) maybeLoadMore();
}

async function loadNewer() {
  const after = newest();
  if (!after || tl.loading) return;
  const gen = tl.gen;
  tl.loading = 'newer';
  try {
    const res = await api.messages(tl.chat, { after, limit: PAGE });
    if (gen !== tl.gen) return;
    tl.msgs = tl.msgs.concat(flatten(res.days));
    tl.hasAfter = res.hasAfter;
    render();
  } catch {
    /* ignore */
  } finally {
    if (gen === tl.gen) tl.loading = null;
  }
  if (gen === tl.gen) maybeLoadMore();
}

function maybeLoadMore() {
  if (!tl.chat || tl.loading || scroller.hidden) return;
  if (tl.hasBefore && scroller.scrollTop < 300) loadOlder();
  else if (tl.hasAfter && scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 300) loadNewer();
}

// Refetch the loaded day range (after an external edit or a conflict).
export async function resync() {
  if (!tl.chat || tl.loading) return;
  const gen = tl.gen;
  tl.loading = 'resync';
  try {
    const params = tl.msgs.length ? { from: oldest(), to: tl.hasAfter ? newest() : FAR_FUTURE } : { limit: PAGE };
    const res = await api.messages(tl.chat, params);
    if (gen !== tl.gen) return;
    tl.msgs = flatten(res.days);
    tl.hasBefore = res.hasBefore;
    tl.hasAfter = res.hasAfter;
    tl.rev = res.rev;
    render();
  } catch {
    /* ignore */
  } finally {
    if (gen === tl.gen) tl.loading = null;
  }
}

// Called by the poller with the chat's current revision.
export function checkRev(rev) {
  if (tl.chat && rev && tl.rev && rev !== tl.rev && state.writes === 0 && !tl.loading) resync();
}

function applyRev(rev) {
  if (!rev) return;
  if (rev.before === tl.rev) tl.rev = rev.after;
  else resync();
}

// Replace the messages of the given days (from a mutation response).
function mergeDays(days) {
  const map = new Map((days || []).map((d) => [d.date, d.messages]));
  const out = [];
  const done = new Set();
  for (const m of tl.msgs) {
    if (map.has(m.date)) {
      if (!done.has(m.date)) {
        out.push(...map.get(m.date));
        done.add(m.date);
      }
    } else {
      out.push(m);
    }
  }
  tl.msgs = out;
}

function onSent({ chat, message, rev }) {
  if (chat !== tl.chat) return;
  if (!tl.hasAfter) tl.msgs.push(message);
  applyRev(rev);
  render();
}

/* ---------- Rendering ---------- */

function buildItems() {
  const items = [];
  if (!tl.hasBefore && tl.loading !== 'initial') items.push({ key: `hero:${tl.chat}`, type: 'hero' });
  const all = tl.msgs.map((m) => ({ key: `m:${m.id}:${m.hash}`, type: 'msg', m, status: 'ok' }));
  if (!tl.hasAfter) {
    for (const j of pendingFor(tl.chat)) {
      all.push({ key: `p:${j.id}:${j.status}`, type: 'msg', m: j.msg, status: j.status, pendingId: j.id });
    }
  }
  let prevDate = null;
  for (const it of all) {
    if (it.m.date !== prevDate) {
      items.push({ key: `sep:${it.m.date}`, type: 'sep', date: it.m.date });
      prevDate = it.m.date;
    }
    items.push(it);
  }
  for (let i = 0; i < all.length; i += 1) {
    const cur = all[i];
    cur.ca = i > 0 && sameGroup(all[i - 1].m, cur.m);
    cur.cb = i < all.length - 1 && sameGroup(cur.m, all[i + 1].m);
    cur.showMeta = !cur.cb || cur.m.edited || cur.status !== 'ok';
  }
  return items;
}

const handlers = {
  onMore: (row, anchor) => {
    if (justClosed(anchor)) return;
    showMenu(row, { anchor, align: 'start' });
  },
  onOpenImage: (a) => lightbox(a.url, a.name),
};

function createNode(it) {
  if (it.type === 'sep') {
    return h(
      'div',
      { class: 'sep', dataset: { key: it.key, date: it.date } },
      h('time', { dataset: { date: it.date, fmt: 'sep' } }, separatorText(it.date)),
    );
  }
  if (it.type === 'hero') return buildHero();
  return buildRow(it, handlers);
}

function showChatFolder() {
  api.reveal(tl.chat).catch((e) => toast(e.kind === 'http' ? e.message : "Couldn't open the folder"));
}

function buildHero() {
  const isDefault = isDefaultChat(tl.chat);
  const sep = state.rootDisplay.includes('\\') ? '\\' : '/';
  const path = `${state.rootDisplay}${sep}${tl.chat}`;
  return h(
    'div',
    { class: 'hero', dataset: { key: `hero:${tl.chat}` } },
    avatar(tl.chat, isDefault, 72),
    h('div', { class: 'hero-name' }, tl.chat),
    isDefault ? h('div', { class: 'hero-pill' }, badge(), 'Official chat') : null,
    h(
      'div',
      { class: 'hero-text' },
      isDefault ? 'You can add notes for yourself in this chat. Notes are saved in ' : 'Notes in this chat are saved in ',
      h(
        'span', // not a <button>: it has to wrap like the sentence around it
        {
          class: 'path',
          role: 'button',
          tabindex: '0',
          title: 'Show in folder',
          onclick: showChatFolder,
          onkeydown: (e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              showChatFolder();
            }
          },
        },
        path,
      ),
      '.',
    ),
  );
}

function findAnchor() {
  const top = scroller.getBoundingClientRect().top;
  for (const el of list.children) {
    if (el === topSentinel || el === bottomSentinel) continue;
    const r = el.getBoundingClientRect();
    if (r.bottom > top) return { el, top: r.top };
  }
  return null;
}

export function render() {
  const items = buildItems();
  const anchor = tl.pinned ? null : findAnchor();
  const seen = new Set();
  let ref = topSentinel.nextSibling;
  for (const it of items) {
    let el = nodes.get(it.key);
    if (!el) {
      el = createNode(it);
      nodes.set(it.key, el);
    }
    seen.add(it.key);
    if (it.type === 'msg') {
      el.classList.toggle('ca', it.ca);
      el.classList.toggle('cb', it.cb);
      el.classList.toggle('nometa', !it.showMeta);
      el.classList.toggle('selected', !it.pendingId && tl.sel.has(it.m.id));
    }
    if (el === ref) ref = ref.nextSibling;
    else list.insertBefore(el, ref);
  }
  for (const [key, el] of nodes) {
    if (!seen.has(key)) {
      disposeRow(el);
      el.remove();
      nodes.delete(key);
    }
  }
  if (tl.pinned) scrollToBottom();
  else if (anchor && anchor.el.isConnected) {
    const delta = anchor.el.getBoundingClientRect().top - anchor.top;
    if (delta) setScrollTop(scroller.scrollTop + delta);
  }
  updateDownButton();
}

/* ---------- Scrolling ---------- */

function setScrollTop(v) {
  ignoreScrollUntil = performance.now() + 80;
  scroller.scrollTop = v;
}

export function scrollToBottom() {
  if (!scroller) return;
  setScrollTop(scroller.scrollHeight);
  if (!tl.hasAfter) tl.pinned = true;
  updateDownButton();
}

function updateDownButton() {
  const dist = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
  downBtn.hidden = !tl.hasAfter && dist <= 15;
}

function onScroll() {
  const dist = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
  tl.pinned = dist <= 15 && !tl.hasAfter;
  updateDownButton();
  maybeLoadMore();
  if (performance.now() > ignoreScrollUntil) showPill();
}

function showPill() {
  const r = scroller.getBoundingClientRect();
  const el = document.elementFromPoint(r.left + r.width / 2, r.top + 40);
  const row = el && el.closest('.row, .sep');
  const date = row && row.dataset.date;
  if (!date) {
    pill.classList.remove('show');
    return;
  }
  pill.textContent = separatorText(date);
  pill.classList.add('show');
  clearTimeout(pillTimer);
  pillTimer = setTimeout(() => pill.classList.remove('show'), 3000);
}

export async function goLatest() {
  if (tl.hasAfter) await load(tl.chat);
  else {
    scroller.scrollTo({ top: scroller.scrollHeight, behavior: 'smooth' });
  }
}

function rowById(id) {
  return list.querySelector(`.row[data-id="${CSS.escape(id)}"]`);
}

function reveal(id) {
  const el = rowById(id);
  if (!el) return;
  tl.pinned = false;
  const r = el.getBoundingClientRect();
  const s = scroller.getBoundingClientRect();
  setScrollTop(scroller.scrollTop + (r.top - s.top) - (s.height - r.height) / 2);
  updateDownButton();
  el.classList.remove('flash');
  void el.offsetWidth;
  el.classList.add('flash');
  setTimeout(() => el.classList.remove('flash'), 1300);
}

// Jump to a message (search result) in the current chat.
export async function jumpTo(id, date) {
  if (rowById(id)) {
    reveal(id);
    return;
  }
  await load(tl.chat, { around: date, highlight: id });
}

/* ---------- Message actions ---------- */

function itemForRow(row) {
  if (row.dataset.pending) {
    const job = jobById(row.dataset.pending);
    return job ? { m: job.msg, job } : null;
  }
  const m = tl.msgs.find((x) => x.id === row.dataset.id);
  return m ? { m } : null;
}

function showMenu(row, at) {
  const item = itemForRow(row);
  if (!item) return;
  const { m, job } = item;
  const items = [];
  if (!job && m.attachments.length) items.push({ label: 'Download', icon: 'download', action: () => download(m) });
  if (!job && m.text) items.push({ label: 'Edit', icon: 'edit', action: () => emit('edit', m) });
  if (!job) items.push({ label: 'Select', icon: 'select', action: () => enterSelect(m.id) });
  if (m.text) items.push({ label: 'Copy text', icon: 'copy', action: () => copyText(m.text) });
  if (!job) items.push({ label: 'Info', icon: 'info', action: () => showInfo(m) });
  items.push({ label: 'Delete', icon: 'trash', action: () => (job ? discardPending(job.id) : deleteMessages([m])) });
  if (job && job.status === 'failed') items.push({ label: 'Retry Send', icon: 'retry', action: () => retrySend(job.id) });
  openMenu(items, at);
}

function onContextMenu(e) {
  const row = e.target.closest('.row');
  if (!row || tl.selecting) return;
  if (!e.target.closest('.bubble, .more')) return;
  e.preventDefault();
  showMenu(row, { x: e.clientX, y: e.clientY });
}

function onClick(e) {
  const row = e.target.closest('.row');
  if (!row) return;
  if (tl.selecting) {
    if (row.dataset.id) {
      e.preventDefault();
      toggleSelect(row.dataset.id, e.shiftKey);
    }
    return;
  }
  if ((e.metaKey || e.ctrlKey) && row.dataset.id && e.target.closest('.bubble') && !e.target.closest('a')) {
    e.preventDefault();
    enterSelect(row.dataset.id);
  }
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast('Copied to clipboard');
  } catch {
    toast("Couldn't copy to the clipboard");
  }
}

function download(m) {
  for (const a of m.attachments) {
    const link = h('a', { href: `${a.url}${a.url.includes('?') ? '&' : '?'}download=1`, download: a.name });
    document.body.append(link);
    link.click();
    link.remove();
  }
}

function showInfo(m) {
  const sep = state.rootDisplay.includes('\\') ? '\\' : '/';
  infoDialog({
    title: 'Message details',
    rows: [
      ['Sent', fullTime(m.ts)],
      m.edited ? ['Edited', 'Yes'] : null,
      ['Saved in', `${state.rootDisplay}${sep}${tl.chat}${sep}${m.date}.md`],
      ...m.attachments.map((a) => ['Attachment', `${a.name}${a.size != null ? ` (${formatSize(a.size)})` : ''}`]),
    ],
    actions: [{ label: 'Show in folder', action: () => api.reveal(tl.chat, m.date).catch(() => toast("Couldn't open the folder")) }],
  });
}

export async function deleteMessages(msgs) {
  if (!msgs.length) return;
  const n = msgs.length;
  const ok = await confirmDialog({
    title: n === 1 ? 'Delete selected message?' : `Delete selected ${n} messages?`,
    body:
      n === 1
        ? 'This message will be deleted from your notes. A copy stays in the trash until you empty it in Settings.'
        : 'These messages will be deleted from your notes. Copies stay in the trash until you empty it in Settings.',
    confirm: 'Delete',
    danger: true,
  });
  if (!ok) return;
  const chat = tl.chat;
  state.writes += 1;
  try {
    const res = await api.remove(chat, msgs.map((m) => ({ id: m.id, hash: m.hash })));
    if (chat !== tl.chat) return;
    mergeDays(res.days);
    exitSelect();
    applyRev(res.rev);
    render();
    emit('chats-dirty');
  } catch (e) {
    if (e.kind === 'conflict') {
      toast('This note changed on disk. Try again.');
      if (e.body && e.body.days) mergeDays(e.body.days);
      resync();
    } else {
      toast(e.kind === 'offline' ? "Couldn't delete. Is the Note to Self server running?" : e.message);
    }
  } finally {
    state.writes -= 1;
  }
}

export async function saveEdit(m, text) {
  const chat = tl.chat;
  const row = rowById(m.id);
  if (row) row.classList.add('saving');
  state.writes += 1;
  try {
    const res = await api.edit(chat, m.id, m.hash, text);
    if (chat !== tl.chat) return true;
    mergeDays(res.days);
    applyRev(res.rev);
    render();
    emit('chats-dirty');
    return true;
  } catch (e) {
    if (row) row.classList.remove('saving');
    if (e.kind === 'conflict') {
      toast('This note changed on disk');
      resync();
    } else {
      toast(e.kind === 'offline' ? "Couldn't save the edit. Is the Note to Self server running?" : e.message);
    }
    return false;
  } finally {
    state.writes -= 1;
  }
}

export function lastEditable() {
  for (let i = tl.msgs.length - 1; i >= 0; i -= 1) if (tl.msgs[i].text) return tl.msgs[i];
  return null;
}

export function isAtLatest() {
  return !tl.hasAfter;
}

/* ---------- Select mode ---------- */

export function enterSelect(id) {
  tl.selecting = true;
  tl.sel = new Set(id ? [id] : []);
  tl.selAnchor = id || null;
  scroller.classList.add('selecting');
  render();
  emit('select', { active: true, count: tl.sel.size });
}

export function exitSelect() {
  if (!tl.selecting) return;
  tl.selecting = false;
  tl.sel = new Set();
  tl.selAnchor = null;
  if (scroller) scroller.classList.remove('selecting');
  for (const el of nodes.values()) el.classList.remove('selected');
  emit('select', { active: false, count: 0 });
}

export function isSelecting() {
  return tl.selecting;
}

function toggleSelect(id, range) {
  if (range && tl.selAnchor) {
    const ids = tl.msgs.map((m) => m.id);
    const a = ids.indexOf(tl.selAnchor);
    const b = ids.indexOf(id);
    if (a >= 0 && b >= 0) {
      const [lo, hi] = a < b ? [a, b] : [b, a];
      for (let i = lo; i <= hi; i += 1) tl.sel.add(ids[i]);
    }
  } else {
    if (tl.sel.has(id)) tl.sel.delete(id);
    else tl.sel.add(id);
    tl.selAnchor = id;
  }
  render();
  emit('select', { active: true, count: tl.sel.size });
}

export function deleteSelected() {
  return deleteMessages(tl.msgs.filter((m) => tl.sel.has(m.id)));
}

export function currentChat() {
  return tl.chat;
}
