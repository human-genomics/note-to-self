// Context menu, dialogs, toast, lightbox and emoji picker.

import { h, icon } from './dom.js';
import { loadLocal, saveLocal } from './store.js';
import { EMOJI_CATEGORIES } from './emoji-data.js';

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

/* ---------- Context menu ---------- */

let closeCurrentMenu = null;
let lastClose = { anchor: null, at: 0 };

export function menuIsOpen() {
  return !!closeCurrentMenu;
}

export function closeMenu() {
  if (closeCurrentMenu) closeCurrentMenu();
}

// True if `anchor` just closed its own menu (so a second click toggles it shut).
export function justClosed(anchor) {
  return lastClose.anchor === anchor && performance.now() - lastClose.at < 400;
}

/**
 * items: [{label, icon, action}] or '-' for a separator.
 * at: {x, y} for pointer menus, or {anchor, align: 'start'|'end'} for button menus.
 */
export function openMenu(items, at) {
  closeMenu();
  const el = h('div', { class: 'menu', role: 'menu', tabindex: '-1' });
  for (const it of items) {
    if (!it) continue;
    if (it === '-') {
      el.append(h('div', { class: 'menu-sep', role: 'separator' }));
      continue;
    }
    el.append(
      h(
        'button',
        {
          class: 'menu-item',
          role: 'menuitem',
          onclick: () => {
            closeMenu();
            it.action();
          },
        },
        it.icon ? icon(it.icon) : null,
        h('span', null, it.label),
      ),
    );
  }
  document.body.append(el);

  const r = el.getBoundingClientRect();
  let left;
  let top;
  if (at.anchor) {
    const a = at.anchor.getBoundingClientRect();
    left = at.align === 'end' ? a.right - r.width : a.left;
    top = a.bottom + 4;
    if (top + r.height > innerHeight - 8) top = a.top - r.height - 4;
    at.anchor.classList.add('open');
  } else {
    left = at.x;
    top = at.y;
    if (left + r.width > innerWidth - 8) left = at.x - r.width;
    if (top + r.height > innerHeight - 8) top = at.y - r.height;
  }
  el.style.left = `${clamp(left, 8, innerWidth - r.width - 8)}px`;
  el.style.top = `${clamp(top, 8, innerHeight - r.height - 8)}px`;

  const buttons = [...el.querySelectorAll('.menu-item')];
  let active = -1;
  const setActive = (i) => {
    active = (i + buttons.length) % buttons.length;
    buttons.forEach((b, j) => b.classList.toggle('active', j === active));
    buttons[active].focus({ preventScroll: true });
  };

  const onDown = (e) => {
    if (!el.contains(e.target)) close();
  };
  const onKey = (e) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      close();
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActive(active + 1);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActive(active < 0 ? buttons.length - 1 : active - 1);
    } else if (e.key === 'Tab') {
      e.preventDefault();
    }
  };
  const close = () => {
    document.removeEventListener('pointerdown', onDown, true);
    document.removeEventListener('keydown', onKey, true);
    document.removeEventListener('scroll', close, true);
    window.removeEventListener('blur', close);
    window.removeEventListener('resize', close);
    if (at.anchor) {
      at.anchor.classList.remove('open');
      lastClose = { anchor: at.anchor, at: performance.now() };
    }
    el.remove();
    closeCurrentMenu = null;
  };
  document.addEventListener('pointerdown', onDown, true);
  document.addEventListener('keydown', onKey, true);
  document.addEventListener('scroll', close, true);
  window.addEventListener('blur', close);
  window.addEventListener('resize', close);
  closeCurrentMenu = close;
  el.focus({ preventScroll: true });
}

/* ---------- Dialogs ---------- */

function makeDialog(wide) {
  const d = h('dialog', { class: wide ? 'dlg wide' : 'dlg' });
  d.addEventListener('click', (e) => {
    if (e.target === d) d.close();
  });
  d.addEventListener('close', () => setTimeout(() => d.remove(), 200));
  document.body.append(d);
  return d;
}

export function dialogIsOpen() {
  return !!document.querySelector('dialog[open]');
}

// focusCancel: for things that can't be undone, so a stray Enter doesn't confirm them.
export function confirmDialog({ title, body, confirm = 'OK', cancel = 'Cancel', danger = false, focusCancel = false }) {
  return new Promise((resolve) => {
    const d = makeDialog(false);
    let ok = false;
    const yes = h(
      'button',
      {
        class: `dlg-btn ${danger ? 'danger' : 'primary'}`,
        onclick: () => {
          ok = true;
          d.close();
        },
      },
      confirm,
    );
    const no = h('button', { class: 'dlg-btn secondary', onclick: () => d.close() }, cancel);
    d.append(
      h('div', { class: 'dlg-body' }, h('h2', { class: 'dlg-title' }, title), body ? h('p', { class: 'dlg-text' }, body) : null),
      h('div', { class: 'dlg-foot' }, no, yes),
    );
    d.addEventListener('close', () => resolve(ok));
    d.showModal();
    (focusCancel ? no : yes).focus();
  });
}

// A larger dialog with free-form content and a Close button (Settings).
export function panelDialog({ title, cls = '' }) {
  const d = makeDialog(true);
  if (cls) d.classList.add(cls);
  const body = h('div', { class: 'dlg-body' });
  d.append(
    h('h2', { class: 'dlg-title panel-title' }, title),
    body,
    h('div', { class: 'dlg-foot' }, h('button', { class: 'dlg-btn secondary', onclick: () => d.close() }, 'Close')),
  );
  d.showModal();
  return { dialog: d, body, close: () => d.close() };
}

/**
 * Text prompt. `submit(value)` may return an error string to keep the dialog open.
 */
export function promptDialog({ title, value = '', placeholder = '', confirm = 'Save', submit }) {
  return new Promise((resolve) => {
    const d = makeDialog(true);
    let result = null;
    const input = h('input', { name: 'name', class: 'dlg-input', type: 'text', autocomplete: 'off', placeholder, maxlength: '64', spellcheck: 'false' });
    input.value = value;
    const err = h('div', { class: 'dlg-error' });
    const yes = h('button', { class: 'dlg-btn primary', type: 'submit' }, confirm);
    const sync = () => {
      yes.disabled = !input.value.trim() || input.value.trim() === value.trim();
      err.textContent = '';
    };
    input.addEventListener('input', sync);
    const form = h(
      'form',
      {
        method: 'dialog',
        onsubmit: async (e) => {
          e.preventDefault();
          if (yes.disabled) return;
          yes.disabled = true;
          const v = input.value.trim();
          const problem = submit ? await submit(v) : null;
          if (problem) {
            err.textContent = problem;
            yes.disabled = false;
            input.focus();
            return;
          }
          result = v;
          d.close();
        },
      },
      h('div', { class: 'dlg-body' }, h('h2', { class: 'dlg-title' }, title), input, err),
      h(
        'div',
        { class: 'dlg-foot' },
        h('button', { class: 'dlg-btn secondary', type: 'button', onclick: () => d.close() }, 'Cancel'),
        yes,
      ),
    );
    d.append(form);
    d.addEventListener('close', () => resolve(result));
    sync();
    d.showModal();
    input.focus();
    input.select();
  });
}

export function infoDialog({ title, rows, actions = [] }) {
  const d = makeDialog(true);
  const dl = h('dl', { class: 'info-rows' });
  for (const row of rows) {
    if (!row) continue;
    dl.append(h('dt', null, row[0]), h('dd', null, row[1]));
  }
  d.append(
    h('div', { class: 'dlg-body' }, h('h2', { class: 'dlg-title' }, title), dl),
    h(
      'div',
      { class: 'dlg-foot' },
      actions.map((a) => h('button', { class: 'dlg-btn secondary', onclick: () => a.action() }, a.label)),
      h('button', { class: 'dlg-btn primary', onclick: () => d.close() }, 'Close'),
    ),
  );
  d.showModal();
}

/* ---------- Toast ---------- */

let toastEl = null;
let toastTimer = 0;

export function toast(message, ms = 4000) {
  if (!toastEl) {
    toastEl = h('div', { class: 'toast', role: 'status', 'aria-live': 'polite' });
    document.body.append(toastEl);
  }
  toastEl.textContent = message;
  // Force a reflow so the fade-in restarts for back-to-back toasts.
  toastEl.classList.remove('show');
  void toastEl.offsetWidth;
  toastEl.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove('show'), ms);
}

/* ---------- Lightbox ---------- */

export function lightbox(url, name) {
  const close = () => {
    document.removeEventListener('keydown', onKey, true);
    el.remove();
  };
  const onKey = (e) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      close();
    }
  };
  const img = h('img', { src: url, alt: name || '' });
  const el = h(
    'div',
    { class: 'lightbox', role: 'dialog', 'aria-label': name || 'Image' },
    h(
      'div',
      { class: 'lb-head' },
      h('div', { class: 'lb-name' }, name || ''),
      h('a', { class: 'ib round', href: url, download: name || '', title: 'Download', 'aria-label': 'Download' }, icon('download')),
      h('button', { class: 'ib round', title: 'Close', 'aria-label': 'Close', onclick: close }, icon('x')),
    ),
    h('div', { class: 'lb-body', onclick: (e) => e.target === e.currentTarget && close() }, img),
  );
  document.addEventListener('keydown', onKey, true);
  document.body.append(el);
}

/* ---------- Emoji picker ---------- */

let closePicker = null;

export function pickerIsOpen() {
  return !!closePicker;
}

export function emojiPicker(anchor, onPick) {
  if (closePicker) {
    closePicker();
    return;
  }
  const recents = loadLocal('emojiRecents', []);
  const cats = recents.length ? [{ name: 'Recently used', tab: '🕘', emoji: recents }, ...EMOJI_CATEGORIES] : EMOJI_CATEGORIES;
  const scroll = h('div', { class: 'ep-scroll' });
  const sections = [];
  for (const cat of cats) {
    const grid = h('div', { class: 'ep-grid' });
    for (const e of cat.emoji) {
      grid.append(
        h(
          'button',
          {
            class: 'ep-cell',
            title: e,
            onclick: () => {
              const next = [e, ...loadLocal('emojiRecents', []).filter((x) => x !== e)].slice(0, 24);
              saveLocal('emojiRecents', next);
              close();
              onPick(e);
            },
          },
          e,
        ),
      );
    }
    const sec = h('section', null, h('div', { class: 'ep-head' }, cat.name), grid);
    sections.push(sec);
    scroll.append(sec);
  }
  const tabs = h(
    'div',
    { class: 'ep-tabs' },
    cats.map((cat, i) =>
      h('button', { class: 'ep-tab', title: cat.name, onclick: () => (scroll.scrollTop = sections[i].offsetTop) }, cat.tab),
    ),
  );
  const el = h('div', { class: 'emoji-pop', role: 'dialog', 'aria-label': 'Emoji' }, scroll, tabs);
  document.body.append(el);
  const a = anchor.getBoundingClientRect();
  el.style.left = `${clamp(a.left, 8, innerWidth - 352 - 8)}px`;
  el.style.top = `${clamp(a.top - 476 - 8, 8, innerHeight - 476 - 8)}px`;

  const syncTabs = () => {
    let cur = 0;
    sections.forEach((s, i) => {
      if (s.offsetTop <= scroll.scrollTop + 4) cur = i;
    });
    [...tabs.children].forEach((t, i) => t.classList.toggle('on', i === cur));
  };
  scroll.addEventListener('scroll', syncTabs);
  syncTabs();

  const onDown = (e) => {
    if (!el.contains(e.target) && !anchor.contains(e.target)) close();
  };
  const onKey = (e) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      close();
    }
  };
  const close = () => {
    document.removeEventListener('pointerdown', onDown, true);
    document.removeEventListener('keydown', onKey, true);
    el.remove();
    closePicker = null;
  };
  document.addEventListener('pointerdown', onDown, true);
  document.addEventListener('keydown', onKey, true);
  closePicker = close;
}

/* ---------- Gate ---------- */

// A card that replaces the whole window: locked out (no key), or all data deleted.
export function gate({ title, lines, art }) {
  closeMenu();
  for (const d of document.querySelectorAll('dialog[open]')) d.close();
  document.querySelector('.gate')?.remove();
  document.body.append(
    h(
      'div',
      { class: 'gate', role: 'alertdialog', 'aria-label': title },
      h('div', { class: 'gate-card' }, art, h('h1', { class: 'gate-title' }, title), lines.map((parts) => h('p', { class: 'gate-text' }, parts))),
    ),
  );
}
