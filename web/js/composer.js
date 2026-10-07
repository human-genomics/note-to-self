// Composer: auto-growing input, per-chat drafts, attachment staging,
// edit mode (✕ / ✓) and the select-mode action bar.

import { h, icon } from './dom.js';
import { state, on, emit, getDraft, setDraft } from './store.js';
import { emojiPicker, toast, confirmDialog, dialogIsOpen, menuIsOpen, pickerIsOpen } from './overlays.js';
import { enqueue } from './sendq.js';
import { normalizeText, utf8Length } from './text.js';
import * as timeline from './timeline.js';

const MAX_TEXT = 65536;
const MAX_FILE = 100 * 1024 * 1024;
const MAX_FILES = 32;

let root;
let bar;
let ta;
let emojiBtn;
let micBtn;
let plusBtn;
let discardBtn;
let confirmBtn;
let editLabel;
let stagingEl;
let selectBar;
let selectCount;
let selectDelete;
let fileInput;
let chat = null;
let edit = null; // {m, orig}
let draftTimer = 0;
let keyboardNav = false;
const staged = new Map(); // chat -> [{id, file, url, isImage, w, h}]
let stageSeq = 0;

const supportsFieldSizing = CSS.supports('field-sizing', 'content');

export function mount(parent, conv) {
  ta = h('textarea', { name: 'message', rows: '1', placeholder: 'Message', spellcheck: 'true', 'aria-label': 'Message' });
  emojiBtn = h('button', { class: 'ib round first', title: 'Add an emoji', 'aria-label': 'Add an emoji' }, icon('emoji'));
  micBtn = h('button', { class: 'ib round', title: 'Voice message', 'aria-label': 'Voice message' }, icon('mic'));
  plusBtn = h('button', { class: 'ib round last', title: 'Add attachment', 'aria-label': 'Add attachment' }, icon('plus'));
  discardBtn = h('button', { class: 'circle-btn discard', title: 'Discard message', 'aria-label': 'Discard message', hidden: true }, icon('x'));
  confirmBtn = h('button', { class: 'circle-btn confirm', title: 'Send edited message', 'aria-label': 'Send edited message', hidden: true }, icon('check'));
  bar = h('div', { class: 'cmp-bar' }, emojiBtn, h('div', { class: 'cmp-input' }, ta), micBtn, plusBtn, discardBtn, confirmBtn);
  editLabel = h('div', { class: 'cmp-edit-label', hidden: true }, icon('edit'), 'Edit message');
  stagingEl = h('div', { class: 'staging', hidden: true });
  selectCount = h('div', { class: 'count' }, '0 selected');
  selectDelete = h('button', { class: 'ib', title: 'Delete', 'aria-label': 'Delete selected messages', onclick: () => timeline.deleteSelected() }, icon('trash'));
  selectBar = h(
    'div',
    { class: 'selectbar', hidden: true },
    h('button', { class: 'ib', title: 'Exit select mode', 'aria-label': 'Exit select mode', onclick: () => timeline.exitSelect() }, icon('x')),
    selectCount,
    selectDelete,
  );
  fileInput = h('input', { name: 'attachments', type: 'file', multiple: true, hidden: true });
  root = h('div', { class: 'composer' }, editLabel, stagingEl, bar, selectBar, fileInput);
  parent.append(root);

  ta.addEventListener('keydown', onKeyDown);
  ta.addEventListener('input', onInput);
  ta.addEventListener('paste', onPaste);
  ta.addEventListener('focus', () => ta.classList.toggle('kfocus', keyboardNav));
  ta.addEventListener('blur', () => ta.classList.remove('kfocus'));
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Tab') keyboardNav = true;
  }, true);
  document.addEventListener('pointerdown', () => {
    keyboardNav = false;
  }, true);

  emojiBtn.addEventListener('click', () => emojiPicker(emojiBtn, insertText));
  micBtn.addEventListener('click', () => toast("Voice notes aren't supported"));
  plusBtn.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => {
    addFiles([...fileInput.files]);
    fileInput.value = '';
  });
  discardBtn.addEventListener('click', cancelEdit);
  confirmBtn.addEventListener('click', submit);

  setupDrop(conv);
  on('edit', (m) => startEdit(m));
  on('select', ({ active, count }) => {
    selectBar.hidden = !active;
    bar.hidden = active;
    editLabel.hidden = active || !edit;
    stagingEl.hidden = active || !current().length;
    selectCount.textContent = `${count} selected`;
    selectDelete.disabled = count === 0;
  });
}

function current() {
  return staged.get(chat) || [];
}

export function setChat(name) {
  if (edit) cancelEdit();
  flushDraft();
  chat = name;
  const d = getDraft(name);
  ta.value = d ? d.text : '';
  autoGrow();
  renderStaging();
  syncButtons();
}

export function focus() {
  if (!bar.hidden) ta.focus();
}

function flushDraft() {
  if (!chat || edit) return;
  clearTimeout(draftTimer);
  setDraft(chat, ta.value);
}

function onInput() {
  autoGrow();
  syncButtons();
  if (edit) return;
  clearTimeout(draftTimer);
  const forChat = chat;
  draftTimer = setTimeout(() => {
    setDraft(forChat, ta.value);
    emit('draft', forChat);
  }, 100);
}

function autoGrow() {
  if (supportsFieldSizing) return;
  ta.style.height = 'auto';
  ta.style.height = `${Math.min(ta.scrollHeight + 2, 74)}px`;
}

function syncButtons() {
  const editing = !!edit;
  const empty = !ta.value.length && !current().length;
  micBtn.hidden = editing || !empty;
  plusBtn.hidden = editing;
  discardBtn.hidden = !editing;
  confirmBtn.hidden = !editing;
  editLabel.hidden = !editing;
  if (editing) confirmBtn.disabled = normalizeText(ta.value) === edit.orig || !normalizeText(ta.value);
}

function insertText(s) {
  ta.focus();
  ta.setRangeText(s, ta.selectionStart, ta.selectionEnd, 'end');
  ta.dispatchEvent(new Event('input'));
}

function onKeyDown(e) {
  if (e.isComposing || e.keyCode === 229) return;
  if (e.key === 'Enter') {
    if (e.shiftKey && !(e.metaKey || e.ctrlKey)) return; // newline
    e.preventDefault();
    submit();
    return;
  }
  if (e.key === 'Escape' && edit) {
    e.preventDefault();
    e.stopPropagation();
    cancelEdit();
    return;
  }
  if (e.key === 'ArrowUp' && !e.shiftKey && !e.altKey && !e.metaKey && !e.ctrlKey && !edit && !ta.value && !current().length) {
    const m = timeline.lastEditable();
    if (m) {
      e.preventDefault();
      startEdit(m);
    }
  }
}

async function submit() {
  if (edit) {
    const text = normalizeText(ta.value);
    if (!text || text === edit.orig) return;
    if (utf8Length(text) > MAX_TEXT) {
      toast('Message body is too long.');
      return;
    }
    confirmBtn.disabled = true;
    const ok = await timeline.saveEdit(edit.m, text);
    if (ok) cancelEdit();
    else syncButtons();
    return;
  }
  const text = normalizeText(ta.value);
  const files = current();
  if (!text && !files.length) return;
  if (utf8Length(text) > MAX_TEXT) {
    toast('Message body is too long.');
    return;
  }
  enqueue(chat, text, files);
  staged.delete(chat);
  ta.value = '';
  clearTimeout(draftTimer);
  setDraft(chat, '');
  emit('draft', chat);
  autoGrow();
  renderStaging();
  syncButtons();
  if (timeline.isAtLatest()) timeline.scrollToBottom();
  else timeline.goLatest();
}

/* ---------- Edit mode ---------- */

export async function startEdit(m) {
  if (!m || !m.text) return;
  if (!edit && (ta.value.trim() || current().length)) {
    const ok = await confirmDialog({ title: 'Discard draft?', body: "This action can't be undone.", confirm: 'Discard', danger: true });
    if (!ok) return;
    staged.delete(chat);
    renderStaging();
    setDraft(chat, '');
    emit('draft', chat);
  }
  timeline.exitSelect();
  edit = { m, orig: m.text };
  ta.value = m.text;
  autoGrow();
  syncButtons();
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
}

function cancelEdit() {
  edit = null;
  ta.value = '';
  autoGrow();
  syncButtons();
}

export function isEditing() {
  return !!edit;
}

/* ---------- Attachments ---------- */

function readImageSize(url) {
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = () => resolve({ w: img.naturalWidth, h: img.naturalHeight });
    img.onerror = () => resolve({});
    img.src = url;
  });
}

export async function addFiles(files) {
  if (!files.length || !chat || edit) return;
  const list = current().slice();
  for (const file of files) {
    if (list.length >= MAX_FILES) {
      toast(`You can attach up to ${MAX_FILES} files at once.`);
      break;
    }
    if (file.size > MAX_FILE) {
      toast(`${file.name} is larger than 100 MB.`);
      continue;
    }
    const url = URL.createObjectURL(file);
    const isImage = /^image\/(png|jpeg|gif|webp|avif|bmp)$/.test(file.type);
    const dims = isImage ? await readImageSize(url) : {};
    list.push({ id: ++stageSeq, file, url, isImage, ...dims });
  }
  staged.set(chat, list);
  renderStaging();
  syncButtons();
  ta.focus();
}

function removeStaged(id) {
  const list = current().filter((s) => {
    if (s.id === id) URL.revokeObjectURL(s.url);
    return s.id !== id;
  });
  if (list.length) staged.set(chat, list);
  else staged.delete(chat);
  renderStaging();
  syncButtons();
}

function renderStaging() {
  const list = current();
  stagingEl.hidden = !list.length || !selectBar.hidden;
  stagingEl.replaceChildren(
    ...list.map((s) =>
      h(
        'div',
        { class: s.isImage ? 'tile' : 'tile file', title: s.file.name },
        s.isImage ? h('img', { src: s.url, alt: s.file.name }) : [icon('file'), h('span', null, s.file.name)],
        h('button', { class: 'tile-rm', title: 'Remove attachment', 'aria-label': 'Remove attachment', onclick: () => removeStaged(s.id) }, icon('x')),
      ),
    ),
  );
}

function onPaste(e) {
  const files = [...(e.clipboardData?.files || [])];
  if (!files.length) return;
  const hasText = !!e.clipboardData.getData('text/plain');
  if (!hasText) e.preventDefault();
  addFiles(files);
}

function setupDrop(conv) {
  const overlay = h('div', { class: 'drop', hidden: true }, 'Drop files to attach');
  conv.append(overlay);
  let depth = 0;
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes('Files');
  conv.addEventListener('dragenter', (e) => {
    if (!hasFiles(e) || edit || !selectBar.hidden) return;
    e.preventDefault();
    depth += 1;
    overlay.hidden = false;
  });
  conv.addEventListener('dragover', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = edit ? 'none' : 'copy';
  });
  conv.addEventListener('dragleave', (e) => {
    if (!hasFiles(e)) return;
    depth = Math.max(0, depth - 1);
    if (!depth) overlay.hidden = true;
  });
  conv.addEventListener('drop', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth = 0;
    overlay.hidden = true;
    addFiles([...e.dataTransfer.files]);
  });
}

// Typing anywhere in the app goes to the composer, as in Signal.
export function handleStrayKey(e) {
  if (bar.hidden || dialogIsOpen() || menuIsOpen() || pickerIsOpen()) return;
  const t = e.target;
  if (t && (t.closest('input, textarea, [contenteditable]') || t.closest('dialog'))) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key.length === 1) ta.focus();
}

export function hasUnsavedState() {
  return !!edit || state.writes > 0;
}
