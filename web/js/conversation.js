// Conversation pane: header (avatar, title, "Official chat"), timeline, composer.

import { h, icon, avatar, badge } from './dom.js';
import { api } from './api.js';
import { state, emit, isDefaultChat, moveDraft } from './store.js';
import { openMenu, justClosed, confirmDialog, promptDialog, toast } from './overlays.js';
import * as timeline from './timeline.js';
import * as composer from './composer.js';

let head;
let searchBtn;
let moreBtn;
let onSearchChat = () => {};

export function mount(conv, { onSearch }) {
  onSearchChat = onSearch;
  head = h('header', { class: 'cv-head' });
  conv.append(head);
  timeline.mount(conv);
  composer.mount(conv, conv);
}

function renderHeader() {
  const name = state.current;
  const isDefault = isDefaultChat(name);
  searchBtn = h('button', { class: 'ib round', title: 'Search', 'aria-label': 'Search in chat', onclick: () => onSearchChat() }, icon('search'));
  moreBtn = h('button', { class: 'ib round', title: 'More', 'aria-label': 'More options' }, icon('more-lg'));
  moreBtn.addEventListener('click', () => {
    if (justClosed(moreBtn)) return;
    chatMenu(name, { anchor: moreBtn, align: 'end' });
  });
  head.replaceChildren(
    h(
      'div',
      { class: 'cv-who' },
      avatar(name, isDefault, 32),
      h(
        'div',
        { class: 'cv-titles' },
        h('div', { class: 'cv-title' }, name, isDefault ? badge() : null),
        isDefault ? h('div', { class: 'cv-sub' }, 'Official chat') : null,
      ),
    ),
    h('div', { class: 'cv-actions' }, searchBtn, moreBtn),
  );
}

export async function openChat(name, { jumpTo } = {}) {
  if (name === state.current && jumpTo) {
    await timeline.jumpTo(jumpTo.id, jumpTo.date);
    return;
  }
  if (name === state.current) return;
  state.current = name;
  try {
    history.replaceState(null, '', `${location.search}#${encodeURIComponent(name)}`);
  } catch {
    /* ignore */
  }
  renderHeader();
  composer.setChat(name);
  emit('chat-opened', name);
  await timeline.load(name, jumpTo ? { around: jumpTo.date, highlight: jumpTo.id } : {});
}

export function refreshHeader() {
  if (state.current) renderHeader();
}

/* ---------- Chat management ---------- */

export function chatMenu(name, at) {
  const isDefault = isDefaultChat(name);
  openMenu(
    [
      {
        label: 'Show in folder',
        icon: 'folder',
        action: () => api.reveal(name).catch((e) => toast(e.kind === 'http' ? e.message : "Couldn't open the folder")),
      },
      isDefault ? null : { label: 'Rename chat', icon: 'rename', action: () => renameChat(name) },
      isDefault ? null : { label: 'Delete chat', icon: 'trash', action: () => deleteChat(name) },
    ],
    at,
  );
}

export async function newChat() {
  let created = null;
  await promptDialog({
    title: 'New chat',
    placeholder: 'Chat name',
    confirm: 'Create',
    submit: async (v) => {
      try {
        const res = await api.createChat(v);
        created = res.chat.name;
        return null;
      } catch (e) {
        return e.kind === 'offline' ? "Can't reach the Note to Self server." : e.message;
      }
    },
  });
  if (created) {
    emit('chats-dirty');
    await openChat(created);
  }
}

async function renameChat(name) {
  let renamed = null;
  await promptDialog({
    title: 'Rename chat',
    value: name,
    confirm: 'Save',
    submit: async (v) => {
      state.chatOps += 1;
      try {
        const res = await api.renameChat(name, v);
        renamed = res.chat.name;
        moveDraft(name, renamed);
        if (state.current === name) state.current = renamed;
        return null;
      } catch (e) {
        return e.kind === 'offline' ? "Can't reach the Note to Self server." : e.message;
      } finally {
        state.chatOps -= 1;
      }
    },
  });
  if (!renamed) return;
  emit('chats-dirty');
  if (state.current === renamed) {
    // Re-open under the new name (the timeline and composer are keyed by chat name).
    state.current = null;
    await openChat(renamed);
  }
}

async function deleteChat(name) {
  const ok = await confirmDialog({
    title: 'Delete chat?',
    body: `“${name}” and all of its notes will be moved to the trash. You can empty the trash in Settings.`,
    confirm: 'Delete',
    danger: true,
  });
  if (!ok) return;
  state.chatOps += 1;
  try {
    await api.deleteChat(name);
  } catch (e) {
    toast(e.kind === 'offline' ? "Can't reach the Note to Self server." : e.message);
    return;
  } finally {
    state.chatOps -= 1;
  }
  emit('chats-dirty');
  if (state.current === name) {
    state.current = null;
    await openChat(state.defaultChat);
  }
}
