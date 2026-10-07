// Settings: where everything is stored, what's there, Empty trash, and Delete all data.

import { h, icon, avatar } from './dom.js';
import { api } from './api.js';
import { state, clearLocal, dropAllDrafts, flushDrafts } from './store.js';
import { hasPending } from './sendq.js';
import { formatSize } from './format.js';
import { confirmDialog, panelDialog, toast, gate } from './overlays.js';

const plural = (n, one, many = `${one}s`) => `${n.toLocaleString()} ${n === 1 ? one : many}`;

function errorText(e) {
  return e.kind === 'offline' ? "Can't reach the Note to Self server." : e.message;
}

export async function openSettings() {
  if (document.querySelector('dialog.settings')) return;
  const panel = panelDialog({ title: 'Settings', cls: 'settings' });
  panel.body.append(h('p', { class: 'set-text' }, 'Loading…'));
  let st;
  let prefs;
  try {
    [st, prefs] = await Promise.all([api.storage(), api.settings()]);
  } catch (e) {
    panel.body.replaceChildren(h('p', { class: 'set-text' }, errorText(e)));
    return;
  }
  render(panel, st, prefs);
}

// Where Note to Self opens; saved in the notes folder, used the next time it opens.
function browserSection(prefs) {
  const select = h('select', { class: 'set-select', name: 'browser', 'aria-label': 'Browser' });
  const auto = prefs.auto ? `Automatic: ${prefs.auto.name}, as an app window` : 'Automatic: your default browser';
  select.append(h('option', { value: 'auto' }, auto));
  for (const b of prefs.browsers) select.append(h('option', { value: b.id }, b.appWindow ? `${b.name}, as an app window` : b.name));
  select.append(h('option', { value: 'default' }, 'Your default browser'));
  if (![...select.options].some((o) => o.value === prefs.browser)) {
    select.append(h('option', { value: prefs.browser }, `${prefs.browser} (not installed)`));
  }
  select.value = prefs.browser;
  const openNow = h('button', { class: 'dlg-btn secondary' }, 'Open a window there now');
  select.addEventListener('change', async () => {
    try {
      await api.saveSettings({ browser: select.value });
      toast(`Saved. Next time Note to Self opens in ${select.selectedOptions[0].textContent.replace(/^Automatic: /, '')}.`);
    } catch (e) {
      toast(errorText(e));
    }
  });
  openNow.addEventListener('click', async () => {
    try {
      const res = await api.openWindow();
      toast(res.ok ? 'Opened a new window. You can close this one.' : "Couldn't open a window from here.");
    } catch (e) {
      toast(errorText(e));
    }
  });
  return h(
    'section',
    { class: 'set-sec' },
    h('h3', { class: 'set-head' }, 'Browser'),
    h('p', { class: 'set-text' }, 'Note to Self opens in:'),
    select,
    prefs.env
      ? h('p', { class: 'set-note' }, `NOTETOSELF_BROWSER is set to “${prefs.env}” where Note to Self was started, so that wins over this setting.`)
      : null,
    h('p', { class: 'set-note' }, 'Chrome, Chromium, Edge, Brave and Vivaldi show it as its own app window, without tabs or an address bar. Other browsers open it in a regular tab.'),
    h('div', { class: 'set-btns' }, openNow),
  );
}

function render(panel, st, prefs) {
  const reveal = h('button', { class: 'dlg-btn secondary', onclick: () => api.reveal().catch((e) => toast(errorText(e))) }, icon('folder'), 'Show in folder');
  const copy = h(
    'button',
    {
      class: 'ib set-copy',
      title: 'Copy path',
      'aria-label': 'Copy path',
      onclick: () =>
        navigator.clipboard.writeText(st.root).then(
          () => toast('Copied the folder path'),
          () => toast("Couldn't copy"),
        ),
    },
    icon('copy'),
  );

  const notes = `${plural(st.notes, 'note')} in ${plural(st.chats, 'chat')}`;
  const atts = st.attachments.count ? `${plural(st.attachments.count, 'file')}, ${formatSize(st.attachments.bytes)}` : 'None';
  const emptyTrash = h('button', { class: 'dlg-btn secondary', disabled: !st.trash.bytes }, icon('trash'), 'Empty trash…');
  emptyTrash.addEventListener('click', async () => {
    const ok = await confirmDialog({
      title: 'Empty the trash?',
      body: `Deleted notes, earlier versions of edited notes and the log of sent notes (${formatSize(st.trash.bytes)}) will be permanently deleted.`,
      confirm: 'Empty trash',
      danger: true,
      focusCancel: true,
    });
    if (!ok) return;
    try {
      render(panel, await api.emptyTrash(), prefs);
      toast('Trash emptied');
    } catch (e) {
      toast(errorText(e));
    }
  });

  const wipe = h('button', { class: 'dlg-btn danger' }, 'Delete all data…');
  wipe.addEventListener('click', () => deleteEverything(panel, st, notes));

  panel.body.replaceChildren(
    h(
      'section',
      { class: 'set-sec' },
      h('h3', { class: 'set-head' }, 'Where your notes are'),
      h('p', { class: 'set-text' }, 'Note to Self keeps everything it saves in this one folder on your computer, and nowhere else:'),
      h('div', { class: 'set-path' }, h('code', null, st.root), copy),
      h('div', { class: 'set-btns' }, reveal),
      h(
        'p',
        { class: 'set-note' },
        'Notes are plain Markdown files, one per day, in a folder per chat. Attachments, unsent drafts and the trash are in there too. Nothing is uploaded: there’s no account, and the app only runs on this computer.',
      ),
    ),
    h(
      'section',
      { class: 'set-sec' },
      h('h3', { class: 'set-head' }, 'Storage'),
      h(
        'dl',
        { class: 'info-rows' },
        h('dt', null, 'Notes'),
        h('dd', null, notes),
        h('dt', null, 'Attachments'),
        h('dd', null, atts),
        h('dt', null, 'Drafts'),
        h('dd', null, st.drafts ? String(st.drafts) : 'None'),
        h('dt', null, 'Trash'),
        h('dd', null, st.trash.bytes ? formatSize(st.trash.bytes) : 'Empty'),
        h('dt', null, 'Total'),
        h('dd', null, formatSize(st.totalBytes)),
      ),
      h('p', { class: 'set-note' }, 'The trash keeps deleted notes, earlier versions of edited notes and a log of sent notes, in case you need them back.'),
      h('div', { class: 'set-btns' }, emptyTrash),
    ),
    browserSection(prefs),
    h(
      'section',
      { class: 'set-sec' },
      h('h3', { class: 'set-head' }, 'Delete all data'),
      h(
        'p',
        { class: 'set-text' },
        'Permanently delete the folder above and everything in it, including the trash. Nothing is kept on this computer, and Note to Self stops afterwards.',
      ),
      h('div', { class: 'set-btns' }, wipe),
    ),
    h('p', { class: 'set-about' }, `Note to Self ${state.version} · Not affiliated with Signal`),
  );
}

async function deleteEverything(panel, st, notes) {
  const ok = await confirmDialog({
    title: 'Delete all data?',
    body: `This permanently deletes ${st.root} and everything in it: ${notes}, ${plural(st.attachments.count, 'attachment')}, your drafts and the trash. Nothing is kept on this computer, and it can’t be undone.`,
    confirm: 'Delete everything',
    danger: true,
    focusCancel: true,
  });
  if (!ok) return;
  try {
    await api.wipe();
  } catch (e) {
    toast(errorText(e));
    return;
  }
  state.halted = true;
  dropAllDrafts();
  clearLocal();
  panel.close();
  gate({
    title: 'All data deleted',
    lines: [
      ['Everything Note to Self stored in ', h('code', null, st.root), ' is gone from this computer, and the app has stopped.'],
      ['You can close this window. To start again with an empty notebook, run ', h('code', null, 'notetoself'), '.'],
    ],
    art: h('div', { class: 'gate-done' }, icon('check')),
  });
}

// Quit from the window: stop the server (the notes stay where they are).
export async function quitApp() {
  if (hasPending()) {
    toast('Wait until your notes are saved, then quit.');
    return;
  }
  await flushDrafts();
  try {
    await api.quit();
  } catch (e) {
    toast(errorText(e));
    return;
  }
  state.halted = true;
  gate({
    title: 'Note to Self has stopped',
    lines: [
      ['Your notes are safe in ', h('code', null, state.root), '.'],
      ['To open it again, run ', h('code', null, 'notetoself'), ' or open Note to Self from your applications.'],
    ],
    art: avatar(state.defaultChat, true, 72),
  });
}

export function showLocked() {
  state.halted = true;
  gate({
    title: 'Open Note to Self with its link',
    lines: [
      ['To keep your notes private, they only open through the link Note to Self gives you.'],
      ['Run ', h('code', null, 'notetoself'), ' (or ', h('code', null, './notetoself'), ' in its folder) and it opens a window for you.'],
    ],
    art: avatar(state.defaultChat, true, 72),
  });
}
