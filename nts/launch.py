"""Command line entry: start the server (once) and open a chromeless browser window."""

from __future__ import annotations

import argparse
import errno
import http.client
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import __version__, browsers, desktop
from .server import KEY_HEADER, Server, app_url, display_path
from .store import FolderError, Store, folder_key, folder_settings, is_notes_folder

DEFAULT_PORT = 6683  # "NOTE" on a phone keypad; not on Chrome's blocked-port list
DEFAULT_DIR = os.path.join("~", "NoteToSelf")

# Ports browsers refuse to open (Chrome's list, which includes Firefox's).
UNSAFE_PORTS = {1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53, 69, 77, 79, 87, 95,
                101, 102, 103, 104, 109, 110, 111, 113, 115, 117, 119, 123, 135, 137, 139, 143, 161,
                179, 389, 427, 465, 512, 513, 514, 515, 526, 530, 531, 532, 540, 548, 554, 556, 563,
                587, 601, 636, 989, 990, 993, 995, 1719, 1720, 1723, 2049, 3659, 4045, 4190, 5060,
                5061, 6000, 6566, 6665, 6666, 6667, 6668, 6669, 6679, 6697, 10080}


def say(msg: str, err: bool = False) -> None:
    """print() that never dies on a console that can't encode a character."""
    stream = sys.stderr if err else sys.stdout
    try:
        print(msg, file=stream, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"), file=stream, flush=True)


def interactive() -> bool:
    """True when printing to a terminal (not a log file), so it's fine to show the key."""
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def same_dir(a: str, b: str) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def probe(port: int, key: Optional[str] = None,
          timeout: float = 0.5) -> Tuple[str, Optional[str]]:
    """("ours", root) if Note to Self answers on the port, ("free", None) if nothing listens,
    ("foreign", None) otherwise. The root is only revealed to someone who has the key of
    that folder; otherwise it's ("ours", None)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    headers = {"X-NoteToSelf": "1"}
    if key:
        headers[KEY_HEADER] = key
    req = urllib.request.Request("http://127.0.0.1:%d/api/ping" % port, headers=headers)
    try:
        with opener.open(req, timeout=timeout) as r:
            data = json.loads(r.read(65536).decode("utf-8"))
    except urllib.error.HTTPError:
        return "foreign", None
    except urllib.error.URLError as e:
        if isinstance(e.reason, ConnectionRefusedError):
            return "free", None
        return "foreign", None
    except ConnectionRefusedError:
        return "free", None
    except (OSError, ValueError, http.client.HTTPException):
        return "foreign", None
    if isinstance(data, dict) and data.get("app") == "note-to-self":
        return "ours", data.get("root")
    return "foreign", None


# -- the window ----------------------------------------------------------------

def browser_choice(flag: Optional[str], root: str) -> Optional[str]:
    """--browser, else $NOTETOSELF_BROWSER, else the browser picked in Settings."""
    return flag or os.environ.get(browsers.BROWSER_ENV) or folder_settings(root).get("browser")


def open_window(url: str, choice: Optional[str] = None) -> bool:
    return browsers.open_window(url, choice, warn=say)


def no_desktop_hint(port: int) -> str:
    return ("No desktop session here, so no window was opened. Open the link above in a browser on "
            "this computer, or reach it from another one with:\n    ssh -L %d:127.0.0.1:%d <this computer>\n"
            "and then open the same link there." % (port, port))



# -- main -----------------------------------------------------------------------

def parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="notetoself",
        description="Note to Self: a Signal-style chat with yourself, saved as Markdown files.")
    p.add_argument("--dir", default=DEFAULT_DIR,
                   help="folder for your notes (default: ~/NoteToSelf)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help="local port (default: %d)" % DEFAULT_PORT)
    p.add_argument("--no-open", action="store_true", help="don't open a browser window")
    p.add_argument("--stop", action="store_true",
                   help="stop the Note to Self that's running for this notes folder, and exit")
    p.add_argument("--install-app", action="store_true",
                   help="add Note to Self to your apps (macOS: ~/Applications, so you can keep it "
                        "in the Dock; Linux: the applications menu), and exit")
    p.add_argument("--uninstall-app", action="store_true", help="remove it from your apps, and exit")
    p.add_argument("--background", action="store_true",
                   help="keep running in the background (what the app does when you click it)")
    p.add_argument("--browser", metavar="NAME",
                   help="the browser to open: %s, default (your default browser), or the path "
                        "to one. Normally the one chosen in Settings, or else an app window of "
                        "Chrome, Chromium, Edge or Brave if you have one, or else your default "
                        "browser. Setting %s works too." % (", ".join(browsers.BROWSERS), browsers.BROWSER_ENV))
    p.add_argument("--version", action="version", version="Note to Self " + __version__)
    args = p.parse_args(argv)
    if not 1 <= args.port <= 65535:
        p.error("--port must be between 1 and 65535")
    if args.port in UNSAFE_PORTS:
        p.error("browsers refuse to open port %d; choose another one, such as %d"
                % (args.port, DEFAULT_PORT))
    if args.browser:
        name = args.browser.strip().lower()
        if name not in browsers.CHOICES and not os.path.exists(args.browser):
            p.error("--browser: unknown browser %r (use %s, default, or a path)"
                    % (args.browser, ", ".join(browsers.BROWSERS)))
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    root = os.path.abspath(os.path.expanduser(args.dir))
    port = args.port

    if args.stop:
        return stop(root, port)
    if args.install_app:
        return install_app(args, root)
    if args.uninstall_app:
        removed = desktop.uninstall()
        say("Removed %s." % ", ".join(removed) if removed else "Note to Self wasn't in your apps.")
        return 0
    state, other_root = probe(port, folder_key(root))
    if state == "ours":
        return _already_running(other_root, root, port, args.no_open, args.browser)
    if args.background and not is_notes_folder(root):
        say("%s already has other files in it." % root, err=True)  # say it before going quiet
        return 1

    try:
        server = Server(port)
    except OSError as e:
        in_use = (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1), 10048, 10013)
        if e.errno not in in_use and getattr(e, "winerror", None) not in (10048, 10013):
            say("Couldn't start the server: %s" % e, err=True)
            return 1
        for _ in range(2):  # maybe a twin launch is still starting up
            time.sleep(0.3)
            state, other_root = probe(port, folder_key(root))
            if state == "ours":
                return _already_running(other_root, root, port, args.no_open, args.browser)
        say("Port %d is used by another program; try --port %d." % (port, port + 1), err=True)
        return 1

    if args.background:
        _detach()  # the port is ours now; let whatever started us (the app) finish
    try:
        store = Store(root)
    except (FolderError, OSError) as e:
        server.server_close()
        say(str(e) if isinstance(e, FolderError) else
            "Couldn't use the notes folder %s: %s" % (root, e), err=True)
        return 1
    server.store = store
    url = app_url(port, store.key)
    say("Note to Self is running. Your notes are saved in %s" % display_path(root))
    if interactive():
        say("Open it at %s  (Ctrl+C to stop)" % url)
    else:  # e.g. `brew services`: keep the key out of log files
        say("Run notetoself to open it.")
    if not args.no_open and not open_window(url, browser_choice(args.browser, root)) and interactive():
        say(no_desktop_hint(port))
    restore = _install_stop_handlers()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        got = store.lock.acquire(timeout=5)  # let an in-flight write finish
        try:
            server.server_close()
        finally:
            if got:
                store.lock.release()
        say("\nStopped.")
    else:
        server.server_close()
        if server.wiped:
            say("All data was deleted: %s is gone. Note to Self has stopped." % display_path(root))
        elif server.quitting:
            say("Note to Self was quit. Stopped.")
    finally:
        restore()
    return 0


def install_app(args: argparse.Namespace, root: str) -> int:
    extra = []
    if os.path.normcase(root) != os.path.normcase(os.path.abspath(os.path.expanduser(DEFAULT_DIR))):
        extra += ["--dir", root]
    if args.port != DEFAULT_PORT:
        extra += ["--port", str(args.port)]
    if args.browser:
        extra += ["--browser", args.browser]
    try:
        path = desktop.install(extra)
    except (desktop.DesktopError, OSError) as e:
        say(str(e), err=True)
        return 1
    say("Added Note to Self to your apps: %s" % path)
    if sys.platform == "darwin":
        say("Open it from Launchpad or Spotlight. To keep it in the Dock, drag it there from the "
            "Finder window that just opened.")
        subprocess.run(["open", "-R", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        say("Find it in your applications menu; from there you can pin it to your dock.")
    return 0


def _detach() -> None:
    """Carry on as a background process that outlives whatever started us."""
    if not hasattr(os, "fork"):
        return  # Windows: stay in the foreground
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)


def stop(root: str, port: int) -> int:
    """`notetoself --stop`: ask the running server for this folder to quit."""
    key = folder_key(root)
    state, other_root = probe(port, key)
    if state == "free":
        say("Note to Self isn't running.")
        return 0
    if state != "ours" or not (other_root and key and same_dir(other_root, root)):
        say("What's running on port %d isn't Note to Self for %s; leaving it alone."
            % (port, display_path(root)), err=True)
        return 1
    req = urllib.request.Request(
        "http://127.0.0.1:%d/api/quit" % port, data=b"{}", method="POST",
        headers={"X-NoteToSelf": "1", KEY_HEADER: key, "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        opener.open(req, timeout=10).read()
    except (OSError, http.client.HTTPException) as e:
        say("Couldn't stop Note to Self: %s" % e, err=True)
        return 1
    for _ in range(50):  # until it no longer answers
        if probe(port, timeout=0.2)[0] != "ours":
            break
        time.sleep(0.1)
    say("Stopped Note to Self.")
    return 0


def _install_stop_handlers() -> Callable[[], None]:
    """Make Ctrl+C and `kill` (SIGTERM) stop gracefully, even when the shell started us with
    SIGINT ignored (as non-interactive shells do for background jobs)."""
    import signal

    def interrupt(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    saved = []
    for name, handler in (("SIGINT", signal.default_int_handler), ("SIGTERM", interrupt)):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            saved.append((sig, signal.signal(sig, handler)))
        except (ValueError, OSError):  # not the main thread
            pass

    def restore() -> None:
        for sig, old in saved:
            try:
                signal.signal(sig, old)
            except (ValueError, OSError, TypeError):
                pass
    return restore


def _already_running(other_root: Optional[str], root: str, port: int, no_open: bool,
                     browser: Optional[str] = None) -> int:
    key = folder_key(root)
    if other_root and key and same_dir(other_root, root):
        url = app_url(port, key)
        say("Note to Self is already running. Open it at %s" % url if interactive()
            else "Note to Self is already running.")
        if not no_open and not open_window(url, browser_choice(browser, root)):
            say(no_desktop_hint(port))
        return 0
    say("Note to Self is already running on port %d with a different notes folder (or for "
        "another user). Stop that one, or use --port %d." % (port, port + 1), err=True)
    return 1
