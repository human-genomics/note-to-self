# Contributing

Thanks for helping! Note to Self aims to stay small and dependable: no dependencies, no build step, notes that are always safe on disk, and a look that matches Signal's Note to Self.

## Getting set up

You need Python 3.8 or newer.

```sh
git clone https://github.com/human-genomics/note-to-self.git
cd note-to-self
python3 -m venv .venv
.venv/bin/python -m unittest discover tests     # unit tests (about 10 seconds)
```

The end-to-end tests run the real app (`notetoself` itself, on a temporary folder) in real browsers and check what ends up on disk. They need [Playwright](https://playwright.dev/python/), which is only for the tests:

```sh
.venv/bin/pip install -r e2e/requirements.txt
.venv/bin/python -m playwright install chromium firefox webkit
.venv/bin/python -m e2e --browser chromium,firefox,webkit     # about 15 seconds per browser
.venv/bin/python -m e2e --headed -k settings                  # watch it; just matching tests
```

Screenshots of failures go to `e2e/artifacts/`. Without Playwright, `.venv/bin/python tools/smoke.py` is a quick check that the page renders in your installed Chrome.

To try your changes with demo notes (never your real ones):

```sh
.venv/bin/python tools/seed.py --out demo-notes --profile readme
./notetoself --dir demo-notes --port 6699
```

The server reads the files in `web/` on every request, so reload the window to see interface changes. Restart `notetoself` after changing Python code.

## Guidelines

- **Python standard library only**, compatible with Python 3.8.
- **Plain ES modules**: no frameworks, bundlers or packages. Build the page with `h()` from `web/js/dom.js`, never with `innerHTML` or other HTML strings, and no inline scripts or styles (the Content Security Policy blocks them). `tests/test_frontend_safety.py` checks this.
- **Never lose a note.** Writes are flushed to disk before they're acknowledged, and edits and deletions save the old version to the trash first. See the helpers at the top of `nts/store.py`.
- **Keep the file format stable and readable.** Changes to `nts/grammar.py` need tests; the round trip is fuzz-tested.
- **Everything stays in the notes folder.** Don't store notes or drafts in the browser or anywhere else.
- Add tests with your change: unit tests in `tests/`, and an end-to-end test in `e2e/` for anything you can click. Run both before opening a pull request; CI runs them on Linux, macOS and Windows.
- Use American spelling (color, gray, labeled).

## Reporting bugs

Open an issue with your operating system, browser, how you installed Note to Self, and the output of `notetoself --version`. If you paste terminal output, remove the `?key=…` part of the link. Report security problems privately (see [SECURITY.md](SECURITY.md)).
