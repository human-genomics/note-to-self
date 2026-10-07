// Tiny DOM builders. User content always goes in as text nodes, never as HTML.

import { avatarColors, initials } from './format.js';

const SVGNS = 'http://www.w3.org/2000/svg';

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (k === 'style') Object.assign(el.style, v);
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
      else if (v === true) el.setAttribute(k, '');
      else el.setAttribute(k, v);
    }
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : String(c));
  }
  return el;
}

export function icon(name, cls) {
  const svg = document.createElementNS(SVGNS, 'svg');
  svg.setAttribute('class', cls ? `icon ${cls}` : 'icon');
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS(SVGNS, 'use');
  use.setAttribute('href', `#i-${name}`);
  svg.append(use);
  return svg;
}

export function badge() {
  return icon('badge', 'badge');
}

// Round avatar: the note glyph for the default chat, initials for the rest.
export function avatar(name, isDefault, size = 48) {
  const el = h('div', { class: 'av' });
  el.style.width = el.style.height = `${size}px`;
  if (isDefault) {
    el.style.setProperty('--av-bg', '#dde7fc');
    el.style.setProperty('--av-fg', '#1251d3');
    el.append(icon('note'));
  } else {
    const c = avatarColors(name);
    el.style.setProperty('--av-bg', c.bg);
    el.style.setProperty('--av-fg', c.fg);
    el.style.fontSize = `${Math.ceil(size * 0.45)}px`;
    el.append(h('span', { class: 'av-label' }, initials(name)));
  }
  return el;
}
