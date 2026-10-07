// Time, date, size and avatar formatting, following Signal Desktop's rules.

const params = new URLSearchParams(location.search);
const FIXED_NOW = params.has('now') ? Date.parse(params.get('now')) : NaN;

// "Now" can be pinned with ?now=2026-10-04T19:00:00 for reproducible screenshots.
export const now = () => (Number.isNaN(FIXED_NOW) ? Date.now() : FIXED_NOW);

const MIN = 60_000;
const HOUR = 3_600_000;
const DAY = 86_400_000;

const clockFmt = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' });
const weekdayFmt = new Intl.DateTimeFormat(undefined, { weekday: 'short' });
const monthDayFmt = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' });
const monthDayYearFmt = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
const weekdayMonthDayFmt = new Intl.DateTimeFormat(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
const fullFmt = new Intl.DateTimeFormat(undefined, { dateStyle: 'full', timeStyle: 'medium' });

export function startOfDay(ts) {
  const d = new Date(ts);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

export function localDate(ts) {
  const d = new Date(ts);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

export function dateToTs(date) {
  const [y, m, d] = date.split('-').map(Number);
  return new Date(y, m - 1, d).getTime();
}

// In-bubble time: "Now", "{n}m", then the clock time.
export function bubbleTime(ts) {
  const diff = now() - ts;
  if (diff < MIN) return 'Now';
  if (diff < HOUR) return `${Math.floor(diff / MIN)}m`;
  return clockFmt.format(ts);
}

// Chat-list time: like the bubble today; weekday within 7 days of the same month; then dates.
export function listTime(ts) {
  const n = now();
  const diff = n - ts;
  if (diff < MIN) return 'Now';
  if (diff < HOUR) return `${Math.floor(diff / MIN)}m`;
  if (startOfDay(ts) === startOfDay(n)) return clockFmt.format(ts);
  const a = new Date(ts);
  const b = new Date(n);
  if (diff < 7 * DAY && a.getMonth() === b.getMonth() && a.getFullYear() === b.getFullYear()) {
    return weekdayFmt.format(ts);
  }
  if (diff < 180 * DAY) return monthDayFmt.format(ts);
  return monthDayYearFmt.format(ts);
}

// Timeline date separators: "Today", "Yesterday", "Mon, Sep 29", "Sep 29, 2024".
export function separatorText(date) {
  const t = dateToTs(date);
  const today = startOfDay(now());
  if (t === today) return 'Today';
  const y = new Date(today);
  y.setDate(y.getDate() - 1);
  if (t === y.getTime()) return 'Yesterday';
  if (today - t < 180 * DAY) return weekdayMonthDayFmt.format(t);
  return monthDayYearFmt.format(t);
}

export function fullTime(ts) {
  return fullFmt.format(ts);
}

export function formatSize(bytes) {
  if (bytes == null) return '';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB'];
  let v = bytes / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}

// Two consecutive notes share a bubble group when < 3 minutes apart on the same day.
export function sameGroup(a, b) {
  const d = b.ts - a.ts;
  return d >= 0 && d < 3 * MIN && a.date === b.date;
}

// Signal's 12 avatar color pairs (A100–A210).
const PALETTE = [
  ['#e3e3fe', '#3838f5'],
  ['#dde7fc', '#1251d3'],
  ['#d8e8f0', '#086da0'],
  ['#cde4cd', '#067906'],
  ['#eae0fd', '#661aff'],
  ['#f5e3fe', '#9f00f0'],
  ['#f6d8ec', '#b8057c'],
  ['#f5d7d7', '#be0404'],
  ['#fef5d0', '#836b01'],
  ['#eae6d5', '#7d6f40'],
  ['#d2d2dc', '#4f4f6d'],
  ['#d7d7d9', '#5c5c5c'],
];

export function avatarColors(name) {
  let hash = 0x811c9dc5;
  for (const ch of name.toLowerCase()) {
    hash ^= ch.codePointAt(0);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  const [bg, fg] = PALETTE[hash % PALETTE.length];
  return { bg, fg };
}

const graphemes = new Intl.Segmenter(undefined, { granularity: 'grapheme' });

function firstGrapheme(word) {
  for (const { segment } of graphemes.segment(word)) return segment;
  return '';
}

export function initials(name) {
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (!words.length) return '';
  const first = firstGrapheme(words[0]);
  const last = words.length > 1 ? firstGrapheme(words[words.length - 1]) : '';
  return (first + last).toUpperCase();
}

export function attachmentKind(name, type = '') {
  const ext = (name.split('.').pop() || '').toLowerCase();
  if (/^(png|jpe?g|gif|webp|avif|bmp)$/.test(ext)) return 'image';
  if (/^(mp4|m4v|webm|mov)$/.test(ext)) return 'video';
  if (/^(mp3|m4a|aac|wav|ogg|opus|flac)$/.test(ext)) return 'audio';
  if (/^image\/(png|jpeg|gif|webp|avif|bmp)$/.test(type)) return 'image';
  return 'file';
}
