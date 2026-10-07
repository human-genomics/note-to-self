"""A real Note to Self server on a temporary notes folder, driven in a real browser.

Each test gets a fresh folder, server and browser context (so a fresh cookie jar).
Which browser comes from E2E_BROWSER: chromium (default), firefox, webkit, or chrome (the
Google Chrome installed on this computer). Screenshots of failures go to e2e/artifacts/.
"""

from __future__ import annotations

import atexit
import datetime as dt
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zlib
from typing import Callable

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:  # keep plain `python -m unittest` working without the e2e extras
    raise unittest.SkipTest("Playwright isn't installed: pip install -r e2e/requirements.txt")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from nts.launch import probe  # noqa: E402
from nts.store import folder_key  # noqa: E402

BROWSER = os.environ.get("E2E_BROWSER", "chromium")
HEADED = os.environ.get("E2E_HEADED") == "1"
ARTIFACTS = os.path.join(ROOT, "e2e", "artifacts")

expect.set_options(timeout=10_000)

_playwright = None
_browser = None


def browser():
    global _playwright, _browser
    if _browser is None:
        _playwright = sync_playwright().start()
        if BROWSER == "chrome":
            _browser = _playwright.chromium.launch(channel="chrome", headless=not HEADED)
        else:
            _browser = getattr(_playwright, BROWSER).launch(headless=not HEADED)
        atexit.register(_close_browser)
    return _browser


def _close_browser():
    global _playwright, _browser
    if _browser is not None:
        _browser.close()
        _browser = None
    if _playwright is not None:
        _playwright.stop()
        _playwright = None


def png_bytes(w: int = 4, h: int = 3) -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"".join(b"\x00" + b"\x2c\x6b\xed" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))  # an ephemeral port: never one that browsers refuse
    port = s.getsockname()[1]
    s.close()
    return port


class App:
    """`notetoself` itself, running as its own process on a fresh notes folder (optionally
    filled with demo notes), the way a person runs it."""

    def __init__(self, seed: bool = False) -> None:
        self.tmp = tempfile.mkdtemp(prefix="nts-e2e-")
        self.root = os.path.join(self.tmp, "notes")
        self.log = os.path.join(self.tmp, "notetoself.log")
        if seed:
            now = dt.datetime.now().replace(microsecond=0).isoformat()
            subprocess.run([sys.executable, os.path.join(ROOT, "tools", "seed.py"), "--out", self.root,
                            "--now", now, "--profile", "readme"], check=True, stdout=subprocess.DEVNULL)
        self.port = free_port()
        self.proc = None
        self.start()
        self.key = folder_key(self.root)

    def start(self) -> None:
        with open(self.log, "a") as log:
            self.proc = subprocess.Popen(
                [sys.executable, os.path.join(ROOT, "notetoself.py"), "--no-open",
                 "--port", str(self.port), "--dir", self.root],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        end = time.time() + 15
        while probe(self.port)[0] != "ours":
            if self.proc.poll() is not None or time.time() > end:
                raise RuntimeError("notetoself didn't start:\n" + self.read(self.log))
            time.sleep(0.05)

    def stop(self) -> None:
        """Stop it like Ctrl+C would: every connection goes away."""
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(10)

    def exited(self, timeout: float = 5.0) -> bool:
        try:
            self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            return False
        return True

    def cleanup(self) -> None:
        self.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @property
    def base(self) -> str:
        return "http://127.0.0.1:%d/" % self.port

    @property
    def link(self) -> str:
        """What `notetoself` opens: the address plus the folder's key."""
        return "%s?key=%s" % (self.base, self.key)

    def path(self, *parts: str) -> str:
        return os.path.join(self.root, *parts)

    def today(self, chat: str = "Note to Self") -> str:
        return self.path(chat, dt.date.today().isoformat() + ".md")

    def read(self, path: str) -> str:
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""


class E2ECase(unittest.TestCase):
    seed = False  # set True for demo notes

    def setUp(self) -> None:
        self.app = App(seed=self.seed)
        self.addCleanup(self.app.cleanup)
        self.context = browser().new_context(viewport={"width": 1100, "height": 720})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.addCleanup(self._after)

    def _after(self) -> None:
        outcome = getattr(self, "_outcome", None)
        if outcome is not None and not getattr(outcome, "success", True):
            os.makedirs(ARTIFACTS, exist_ok=True)
            name = "%s-%s.png" % (BROWSER, self.id().rsplit(".", 2)[-2] + "." + self._testMethodName)
            try:
                self.page.screenshot(path=os.path.join(ARTIFACTS, name), full_page=True)
            except Exception:
                pass
        self.assertEqual(self.errors, [], "JavaScript errors on the page")

    # -- driving the app -----------------------------------------------------------

    def open(self) -> None:
        """Open the app the way `notetoself` does, with the key in the link."""
        self.page.goto(self.app.link)
        expect(self.page.locator(".chat-row").first).to_be_visible()
        expect(self.composer).to_be_focused()  # the last thing the app does when it has started

    @property
    def composer(self):
        return self.page.get_by_placeholder("Message")

    def row(self, text: str):
        """The timeline row of the saved note whose text contains `text`."""
        return self.page.locator(".row[data-id]").filter(
            has=self.page.locator(".text", has_text=text)).last

    def send(self, text: str):
        self.composer.fill(text)
        self.composer.press("Enter")
        row = self.row(text.splitlines()[0])
        expect(row.locator(".status.read")).to_be_visible()  # ✓✓: it's on disk
        return row

    def menu(self, row, item: str) -> None:
        row.locator(".bubble").click(button="right")
        self.page.locator(".menu-item", has_text=item).click()

    def dialog_button(self, label: str) -> None:
        self.page.locator("dialog[open]").last.get_by_role("button", name=label, exact=True).click()

    def wait_until(self, check: Callable[[], bool], what: str, timeout: float = 8.0) -> None:
        end = time.time() + timeout
        while time.time() < end:
            if check():
                return
            time.sleep(0.05)
        self.fail("timed out waiting until " + what)

    def wait_for_file(self, path: str, text: str, timeout: float = 8.0) -> None:
        self.wait_until(lambda: text in self.app.read(path), "%s contains %r" % (path, text), timeout)


def key_combo(key: str) -> str:
    """Cmd on macOS, Ctrl elsewhere, like the app's own shortcuts."""
    return "ControlOrMeta+" + key

