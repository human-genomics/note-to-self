// Message text → DOM: links and emoji spans, built from text nodes only.

import { h } from './dom.js';

// Bare domains are linked only for common web TLDs, so file names such as
// prompt.md, setup.py or run.sh stay plain text (Signal would link prompt.md).
const TLDS = 'com|org|net|edu|gov|io|ai|app|dev|co|me|info|us|uk|ca|de|fr|nl|eu|jp|au|in|tv|xyz';
const LINK_RE = new RegExp(
  String.raw`(?<![\w@./-])(?:https?:\/\/[^\s<>"'\x60]+|www\.[^\s<>"'\x60]+|(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:${TLDS})(?![\w-])(?::\d{1,5})?(?:[/?#][^\s<>"'\x60]*)?)`,
  'gi',
);

const EMOJI_TEST = /\p{Emoji_Presentation}|\p{Extended_Pictographic}️|\p{Regional_Indicator}|⃣/u;
const graphemes = new Intl.Segmenter(undefined, { granularity: 'grapheme' });

function trimLink(s) {
  for (;;) {
    const last = s[s.length - 1];
    if (/[.,:;!?'"]/.test(last)) {
      s = s.slice(0, -1);
      continue;
    }
    const pairs = { ')': '(', ']': '[', '}': '{' };
    if (pairs[last]) {
      const open = s.split(pairs[last]).length - 1;
      const close = s.split(last).length - 1;
      if (close > open) {
        s = s.slice(0, -1);
        continue;
      }
    }
    return s;
  }
}

function safeHref(s) {
  const href = /^https?:\/\//i.test(s) ? s : `http://${s}`;
  try {
    const u = new URL(href);
    return u.protocol === 'http:' || u.protocol === 'https:' ? u.href : null;
  } catch {
    return null;
  }
}

// Append text to a node, wrapping emoji graphemes so they render at Signal's larger size.
export function appendEmojified(parent, text) {
  if (!EMOJI_TEST.test(text)) {
    parent.append(text);
    return parent;
  }
  let buf = '';
  let emo = '';
  const flush = () => {
    if (buf) parent.append(buf);
    if (emo) parent.append(h('span', { class: 'emoji' }, emo));
    buf = '';
    emo = '';
  };
  for (const { segment } of graphemes.segment(text)) {
    if (EMOJI_TEST.test(segment)) {
      if (buf) {
        parent.append(buf);
        buf = '';
      }
      emo += segment;
    } else {
      if (emo) {
        parent.append(h('span', { class: 'emoji' }, emo));
        emo = '';
      }
      buf += segment;
    }
  }
  flush();
  return parent;
}

export function renderText(text, { links = true } = {}) {
  const frag = document.createDocumentFragment();
  if (!links) return appendEmojified(frag, text);
  let last = 0;
  LINK_RE.lastIndex = 0;
  for (let m; (m = LINK_RE.exec(text)); ) {
    const raw = trimLink(m[0]);
    const href = safeHref(raw);
    if (!href) continue;
    appendEmojified(frag, text.slice(last, m.index));
    frag.append(h('a', { href, target: '_blank', rel: 'noopener noreferrer' }, raw));
    last = m.index + raw.length;
    LINK_RE.lastIndex = last;
  }
  appendEmojified(frag, text.slice(last));
  return frag;
}

// Search snippet segments [[text, isMatch], ...] → fragment with bold matches.
export function renderSnippet(segments) {
  const frag = document.createDocumentFragment();
  for (const [text, isMatch] of segments || []) {
    if (isMatch) frag.append(appendEmojified(h('b'), text));
    else appendEmojified(frag, text);
  }
  return frag;
}

export function utf8Length(s) {
  return new TextEncoder().encode(s).length;
}

// Mirror the server's normalization so a pending bubble looks like the saved one.
export function normalizeText(s) {
  return s.replace(/\r\n?/g, '\n').replace(/^(?:[ \t]*\n)+/, '').replace(/\s+$/, '');
}
