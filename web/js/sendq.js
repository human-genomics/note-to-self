// Optimistic send queue: shows a pending bubble at once, uploads attachments,
// then saves the note. The server answers only after the note is fsync'd to disk.

import { api } from './api.js';
import { state, emit } from './store.js';
import { attachmentKind, localDate, now } from './format.js';

const jobs = [];
let running = false;
let seq = 0;

export function pendingFor(chat) {
  return jobs.filter((j) => j.chat === chat);
}

export function hasPending() {
  return jobs.length > 0;
}

/** staged: [{file, url (blob:), isImage, w, h}] */
export function enqueue(chat, text, staged) {
  const ts = now();
  const files = staged.map((s) => ({ ...s, stored: null }));
  const job = {
    id: `p${++seq}`,
    chat,
    text,
    files,
    status: 'sending',
    started: false,
    msg: {
      id: '',
      hash: '',
      date: localDate(ts),
      ts,
      edited: false,
      text,
      attachments: files.map((f) => ({
        kind: attachmentKind(f.file.name || '', f.file.type),
        name: f.file.name || 'file',
        size: f.file.size,
        url: f.url,
        w: f.w,
        h: f.h,
      })),
    },
  };
  jobs.push(job);
  emit('pending', chat);
  pump();
  return job;
}

async function pump() {
  if (running) return;
  running = true;
  try {
    for (;;) {
      const job = jobs.find((j) => j.status === 'sending' && !j.started);
      if (!job) break;
      job.started = true;
      state.writes += 1;
      try {
        for (const f of job.files) {
          if (!f.stored) f.stored = await api.upload(job.chat, f.file);
        }
        const res = await api.send(
          job.chat,
          job.text,
          job.files.map((f) => ({ file: f.stored.file, name: f.stored.name })),
        );
        jobs.splice(jobs.indexOf(job), 1);
        emit('sent', { chat: job.chat, job, message: res.message, rev: res.rev });
        setTimeout(() => job.files.forEach((f) => URL.revokeObjectURL(f.url)), 5000);
      } catch (e) {
        job.status = 'failed';
        job.started = false;
        job.error = e;
        emit('pending', job.chat);
      } finally {
        state.writes -= 1;
      }
    }
  } finally {
    running = false;
  }
}

export function retry(id) {
  const job = jobs.find((j) => j.id === id);
  if (!job) return;
  job.status = 'sending';
  job.started = false;
  emit('pending', job.chat);
  pump();
}

export function discard(id) {
  const i = jobs.findIndex((j) => j.id === id);
  if (i < 0) return;
  const [job] = jobs.splice(i, 1);
  job.files.forEach((f) => URL.revokeObjectURL(f.url));
  emit('pending', job.chat);
}

export function jobById(id) {
  return jobs.find((j) => j.id === id) || null;
}
