// Fetch wrapper for the local server. Every /api call carries X-NoteToSelf: 1; the browser's
// key cookie (set when the launcher's link was opened) proves it may read the notes.

export class ApiError extends Error {
  constructor(kind, message, { status = 0, code = '', body = null } = {}) {
    super(message);
    this.kind = kind; // 'offline' | 'conflict' | 'http'
    this.status = status;
    this.code = code;
    this.body = body;
  }
}

const enc = encodeURIComponent;

let onLockedFn = () => {};

// Called when the server says this browser doesn't have the key (status 401, "locked").
export function onLocked(fn) {
  onLockedFn = fn;
}

function qs(params) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) if (v != null && v !== '') p.set(k, v);
  const s = p.toString();
  return s ? `?${s}` : '';
}

async function req(method, path, { json, body, headers = {}, timeout = 10_000, keepalive = false } = {}) {
  const ctrl = new AbortController();
  const timer = timeout ? setTimeout(() => ctrl.abort(), timeout) : 0;
  const init = {
    method,
    headers: { 'X-NoteToSelf': '1', ...headers },
    signal: ctrl.signal,
    cache: 'no-store',
    credentials: 'same-origin',
    keepalive, // lets a last draft save finish while the window closes
  };
  if (json !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(json);
  } else if (body !== undefined) {
    init.body = body;
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch (e) {
    throw new ApiError('offline', "Can't reach the Note to Self server.");
  } finally {
    clearTimeout(timer);
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (!res.ok) {
    const err = (data && data.error) || {};
    const kind = res.status === 409 && err.code === 'conflict' ? 'conflict' : 'http';
    if (res.status === 401 && err.code === 'locked') onLockedFn();
    throw new ApiError(kind, err.message || `Request failed (${res.status})`, {
      status: res.status,
      code: err.code || '',
      body: data,
    });
  }
  return data;
}

export const api = {
  ping: () => req('GET', '/api/ping', { timeout: 3000 }),
  chats: () => req('GET', '/api/chats', { timeout: 5000 }),
  createChat: (name) => req('POST', '/api/chats', { json: { name } }),
  renameChat: (chat, name) => req('PATCH', `/api/chats/${enc(chat)}`, { json: { name } }),
  deleteChat: (chat) => req('DELETE', `/api/chats/${enc(chat)}`),
  messages: (chat, params) => req('GET', `/api/chats/${enc(chat)}/messages${qs(params)}`),
  send: (chat, text, attachments) =>
    req('POST', `/api/chats/${enc(chat)}/messages`, { json: { text, attachments } }),
  edit: (chat, id, hash, text) =>
    req('PATCH', `/api/chats/${enc(chat)}/messages/${enc(id)}`, { json: { text, hash } }),
  remove: (chat, items) => req('POST', `/api/chats/${enc(chat)}/messages/delete`, { json: { items } }),
  upload: (chat, file) =>
    req('POST', `/api/chats/${enc(chat)}/attachments${qs({ name: file.name || 'file' })}`, {
      body: file,
      headers: { 'Content-Type': file.type || 'application/octet-stream' },
      timeout: 0,
    }),
  search: (q, chat) => req('GET', `/api/search${qs({ q, chat })}`),
  reveal: (chat, date) => req('POST', '/api/reveal', { json: { chat, date } }),
  saveDraft: (chat, text, keepalive = false) =>
    req('PUT', `/api/chats/${enc(chat)}/draft`, { json: { text }, keepalive, timeout: keepalive ? 0 : 10_000 }),
  storage: () => req('GET', '/api/storage'),
  emptyTrash: () => req('POST', '/api/trash/empty', { json: {} }),
  wipe: () => req('POST', '/api/wipe', { json: { confirm: 'delete all data' }, timeout: 120_000 }),
  quit: () => req('POST', '/api/quit', { json: {} }),
  settings: () => req('GET', '/api/settings'),
  saveSettings: (changes) => req('PUT', '/api/settings', { json: changes }),
  openWindow: () => req('POST', '/api/open', { json: {} }),
};
