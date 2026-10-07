# Security

Note to Self keeps your notes on your own computer, so its security is about who else on that computer, and which websites, could read or change them.

## How your notes are protected

- **Only on this computer.** The server listens on 127.0.0.1, never on your network, and makes no outgoing connections. The page loads nothing from the internet.
- **A key per notes folder.** The first time it runs, Note to Self saves a random key in `<notes folder>/.notetoself.json`, readable only by you. The launcher opens the window with `/?key=…`; the server swaps that for an `HttpOnly`, `SameSite=Strict` cookie and removes the key from the address. Every API request and attachment needs the key, so other accounts on the same computer can't read your notes through 127.0.0.1. The key is never written to log files (for example under `brew services`).
- **A private folder.** The notes folder is created with permissions `0700` on macOS and Linux.
- **Websites can't reach it.** Every request must have a `Host` of `127.0.0.1:<port>` or `localhost:<port>` (which blocks DNS rebinding) and, when the browser sends `Sec-Fetch-Site`, come from the app itself. API requests also need an `X-NoteToSelf` header, which another site can only send after a CORS preflight that this server never answers.
- **A strict Content Security Policy.** Scripts and styles only come from the app itself; there's no inline code, and the interface never builds HTML from strings (a test enforces this).
- **Sandboxed attachments.** Attachments are served with `Content-Security-Policy: sandbox`. Only images, audio and video are shown inline; everything else, including SVG, HTML and PDF, is downloaded.
- **Strict paths.** URL paths are decoded strictly (`..`, slashes, backslashes and NUL are rejected), and files must resolve inside the notes folder.
- **Nothing in the browser.** API responses and attachments are sent with `Cache-Control: no-store`, and drafts are saved in the notes folder rather than in browser storage.
- **Careful deleting.** Note to Self refuses to use a folder that already holds unrelated files, and "Delete all data" refuses to delete anything that doesn't look like a Note to Self folder (never your home folder).

## What it doesn't protect against

- Anyone who can use your account (or an administrator) can read the folder, like any of your files.
- Notes aren't encrypted on disk. Use your operating system's disk encryption (FileVault, LUKS, BitLocker).
- Backups and sync tools (Time Machine, cloud drives) keep their own copies, which "Delete all data" can't reach.
- On Windows the folder doesn't get special permissions; Windows support is best effort.

## Reporting a vulnerability

Please report security problems privately, through this repository's **Security** tab → **Report a vulnerability**, rather than in a public issue. You'll get a reply as soon as possible.
