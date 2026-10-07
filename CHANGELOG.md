# Changelog

## 0.1.0 (2026-10-07)

The first release.

- A chat with yourself that looks and feels like Signal Desktop's Note to Self (dark theme).
- Every note is saved as Markdown, one file per day in a folder per chat, and gets its ✓✓ only once it's on disk. Edit the files by hand and the app picks up the changes.
- Scroll back through your history, search across chats, edit and delete notes (one or many), attach images and files, keep several chats, and keep a draft per chat.
- Settings shows exactly where everything is stored (`~/NoteToSelf` by default), what's there, and lets you empty the trash or delete all data for good.
- Private by design: nothing is uploaded, other accounts on the same computer and websites can't read your notes, and nothing is kept in the browser.
- Open it like any other app: `notetoself --install-app` adds it to your Applications folder on macOS (for Launchpad, Spotlight and the Dock) or to your applications menu on Linux.
- Opens in its own app window in Chrome, Chromium, Edge, Brave or Vivaldi, or in your default browser; choose the browser in Settings, with `--browser`, or with `NOTETOSELF_BROWSER`. Quit from the ☰ menu or with `notetoself --stop`.
- macOS and Linux (Ubuntu 20.04 and newer) with Python 3.8 or newer; Windows on a best-effort basis. Install with Homebrew, with `apt` from the Note to Self APT repository (or the `.deb`), or run from source.
