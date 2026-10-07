#!/usr/bin/env python3
"""End-to-end smoke test in a real browser.

Seeds demo notes into a temporary folder, starts the server, and loads the app in headless
Chrome (or Chromium/Edge/Brave): once without the key (it must show the "open with its link"
screen) and once through the launcher's link (it must render the chats and notes). This
catches JavaScript errors that the unit tests can't see.

    python3 tools/smoke.py [--browser PATH]
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from nts.browsers import find_browser  # noqa: E402
from nts.server import Server  # noqa: E402
from nts.store import Store  # noqa: E402

NOW = "2026-10-06T19:00:00"


def browser_binary(path: str = None) -> str:
    if path:
        return path
    found = find_browser()
    if not found:
        sys.exit("No Chrome, Chromium, Edge or Brave found; pass --browser PATH.")
    _name, kind, p = found
    if kind == "mac":  # the executable inside the .app bundle
        return os.path.join(p, "Contents", "MacOS", os.path.basename(p)[:-4])
    return p


def dump_dom(binary: str, url: str, profile: str) -> str:
    cmd = [binary, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
           "--user-data-dir=" + profile, "--virtual-time-budget=15000", "--dump-dom", url]
    if os.environ.get("CI") and sys.platform.startswith("linux"):
        cmd.insert(1, "--no-sandbox")  # CI runners don't allow Chrome's sandbox
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    killer = threading.Timer(180, proc.kill)
    killer.start()
    out = []
    try:
        for line in proc.stdout:
            out.append(line)
            if b"</html>" in line:  # the whole page is out
                break
    finally:
        killer.cancel()
        proc.kill()  # headless Chrome on macOS doesn't always exit after --dump-dom
        proc.wait()
    return b"".join(out).decode("utf-8", "replace")


def main(argv: list = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    p.add_argument("--browser", help="path to a Chromium-family browser executable")
    args = p.parse_args(argv)
    binary = browser_binary(args.browser)
    tmp = tempfile.mkdtemp(prefix="nts-smoke-")
    server = None
    try:
        notes = os.path.join(tmp, "notes")
        subprocess.run([sys.executable, os.path.join(ROOT, "tools", "seed.py"), "--out", notes,
                        "--now", NOW, "--profile", "readme"], check=True, stdout=subprocess.DEVNULL)
        store = Store(notes, warm=False)
        server = Server(0, store)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        base = "http://127.0.0.1:%d/" % port

        locked = dump_dom(binary, "%s?now=%s" % (base, NOW), os.path.join(tmp, "profile-a"))
        app = dump_dom(binary, "%s?key=%s&now=%s" % (base, store.key, NOW), os.path.join(tmp, "profile-b"))
        checks = [
            ("without the key, the notes stay locked", "Open Note to Self with its link" in locked
             and "water the plants" not in locked),
            ("with the key, the chat list renders", app.count('class="chat-row') >= 5),
            ("with the key, the notes render", "water the plants on Sunday" in app),
            ("the composer renders", 'placeholder="Message"' in app),
            ("no locked screen with the key", 'class="gate' not in app),
        ]
        width = max(len(name) for name, _ in checks)
        for name, ok in checks:
            print("%s  %s" % ("ok  " if ok else "FAIL", name.ljust(width)))
        if not all(ok for _, ok in checks):
            print("\n--- page with the key ---\n" + app[:3000])
            return 1
        print("Smoke test passed (%s)." % os.path.basename(binary))
        return 0
    finally:
        if server:
            server.shutdown()
            server.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
