// Builds one timeline row (outgoing bubble) for a saved or pending note.

import { h, icon } from './dom.js';
import { bubbleTime, formatSize } from './format.js';
import { renderText } from './text.js';

// Keeps each text spacer as wide as its metadata (+10px), so short last lines
// share a line with the time and long ones push it below — Signal's float trick.
let onSpacersChanged = () => {};

// The timeline keeps itself pinned to the bottom when spacers change bubble heights. (It
// doesn't observe its own list: that would make Safari report a ResizeObserver loop.)
export function setSpacersChangedHandler(fn) {
  onSpacersChanged = fn;
}

export const metaObserver = new ResizeObserver((entries) => {
  let changed = false;
  for (const e of entries) {
    const sp = e.target._spacer;
    if (!sp) continue;
    const w = e.borderBoxSize?.[0]?.inlineSize ?? e.target.offsetWidth;
    const width = `${Math.ceil(w) + 10}px`;
    if (sp.style.width !== width) {
      sp.style.width = width;
      changed = true;
    }
  }
  if (changed) onSpacersChanged();
});

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

function imageBox(a) {
  if (a.w && a.h) {
    const w = clamp(a.w, 200, 300);
    return { w, h: clamp(Math.round((w * a.h) / a.w), 50, 450) };
  }
  return { w: 300, h: 200 };
}

function mediaEl(a, single, onOpen) {
  const box = h('div', { class: 'att-img', dataset: { name: a.name } });
  if (single) {
    const { w, h: ht } = imageBox(a);
    box.style.width = `${w}px`;
    box.style.height = `${ht}px`;
  }
  if (a.kind === 'video') {
    box.append(h('video', { src: a.url, controls: true, preload: 'metadata', playsinline: true }));
    box.style.cursor = 'default';
    return box;
  }
  const img = h('img', { src: a.url, alt: a.name || '', loading: 'lazy', decoding: 'async', draggable: 'false' });
  if (single && !(a.w && a.h)) {
    img.addEventListener('load', () => {
      if (!img.naturalWidth) return;
      const fit = imageBox({ w: img.naturalWidth, h: img.naturalHeight });
      box.style.width = `${fit.w}px`;
      box.style.height = `${fit.h}px`;
    });
  }
  box.append(img);
  box.addEventListener('click', (e) => {
    e.stopPropagation();
    onOpen(a);
  });
  return box;
}

function fileCard(a, withSpacer) {
  const ext = (a.name.includes('.') ? a.name.split('.').pop() : '').slice(0, 4).toLowerCase();
  const size = h('div', { class: 'fc-size' }, formatSize(a.size));
  let sp = null;
  if (withSpacer) {
    sp = h('span', { class: 'sp' });
    size.append(sp);
  }
  const card = h(
    'a',
    { class: 'file-card', href: a.url, download: a.name, title: a.name, draggable: 'false' },
    h('div', { class: 'fc-icon' }, icon('doc'), h('div', { class: 'fc-ext' }, ext)),
    h('div', { class: 'fc-info' }, h('div', { class: 'fc-name' }, a.name), size),
  );
  card.addEventListener('click', (e) => e.stopPropagation());
  return { card, sp };
}

function metaEl(m, status) {
  const inner = h('span', { class: 'meta-in' });
  if (m.edited && status === 'ok') inner.append(h('span', { class: 'edited' }, 'Edited'));
  if (status === 'failed') {
    inner.append(h('span', null, 'Send failed'));
  } else {
    inner.append(h('time', { dataset: { ts: String(m.ts), fmt: 'bubble' } }, bubbleTime(m.ts)));
    if (status === 'sending') inner.append(icon('sending', 'status sending'));
    else inner.append(icon('read', 'status read'));
  }
  return { meta: h('div', { class: 'meta' }, inner), inner };
}

/**
 * item: {key, m, status: 'ok'|'sending'|'failed', pendingId?}
 * handlers: {onMore(row, anchorEl), onOpenImage(attachment)}
 */
export function buildRow(item, handlers) {
  const { m, status } = item;
  const atts = m.attachments || [];
  const media = atts.filter((a) => a.kind === 'image' || a.kind === 'video');
  const audio = atts.filter((a) => a.kind === 'audio');
  const files = atts.filter((a) => a.kind === 'file');
  const hasText = !!m.text;

  const bubble = h('div', { class: 'bubble' });
  if (hasText) bubble.classList.add('has-caption');
  const mediaOnly = !hasText && media.length > 0 && !files.length && !audio.length;
  if (mediaOnly) bubble.classList.add('media-only');

  if (media.length) {
    const single = media.length === 1;
    const grid = h('div', { class: single ? 'atts' : 'atts two' });
    if (!single) grid.style.width = '300px';
    media.forEach((a, i) => {
      const el = mediaEl(a, single, handlers.onOpenImage);
      if (!single && media.length % 2 === 1 && i === media.length - 1) el.classList.add('wide');
      grid.append(el);
    });
    bubble.append(grid);
  }
  for (const a of audio) bubble.append(h('audio', { class: 'att-audio', src: a.url, controls: true, preload: 'metadata' }));

  let spacer = null;
  const allFiles = [...audio, ...files];
  allFiles.forEach((a, i) => {
    const last = i === allFiles.length - 1;
    const { card, sp } = fileCard(a, last && !hasText);
    if (sp) spacer = sp;
    bubble.append(card);
  });

  if (hasText) {
    spacer = h('span', { class: 'sp' });
    bubble.append(h('div', { class: 'text', dir: 'auto' }, renderText(m.text), spacer));
  }

  const { meta, inner } = metaEl(m, status);
  if (spacer) {
    inner._spacer = spacer;
    metaObserver.observe(inner);
  } else if (!mediaOnly) {
    meta.classList.add('block');
  }
  bubble.append(meta);

  const more = h('button', { class: 'more', 'aria-label': 'More actions', title: 'More actions' }, icon('more-lg'));
  const row = h(
    'div',
    { class: 'row', dataset: { key: item.key, date: m.date } },
    h('span', { class: 'check', 'aria-hidden': 'true' }, icon('check')),
    status === 'failed' ? h('div', { class: 'err', title: 'Send failed' }, icon('error')) : null,
    h('div', { class: 'outer' }, bubble),
    more,
  );
  if (item.pendingId) {
    row.dataset.pending = item.pendingId;
    row.classList.add('pending');
    if (status === 'failed') row.classList.add('failed');
  } else {
    row.dataset.id = m.id;
  }
  more.addEventListener('click', (e) => {
    e.stopPropagation();
    handlers.onMore(row, more);
  });
  return row;
}

export function disposeRow(row) {
  const inner = row.querySelector('.meta-in');
  if (inner) metaObserver.unobserve(inner);
}
