"""Finding browsers and opening the app window in one (an app window when the browser can).

Chrome, Chromium, Edge, Brave and Vivaldi can show a page as its own window, without tabs
or an address bar (--app), so they're preferred over the system's default browser.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import shutil
import subprocess
import sys
import webbrowser
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# Browsers by the name you can pass to --browser: (display name, macOS apps, Linux commands or
# paths (snap installs aren't always on PATH, e.g. when started from a shortcut), Windows
# (exe, path under Program Files or AppData), Flatpak app ids).
BROWSERS: Dict[str, Tuple[str, List[str], List[str], List[Tuple[str, str]], List[str]]] = {
    "chrome": ("Google Chrome", ["Google Chrome"], ["google-chrome", "google-chrome-stable"],
               [("chrome.exe", r"Google\Chrome\Application\chrome.exe")], ["com.google.Chrome"]),
    "chromium": ("Chromium", ["Chromium"], ["chromium", "chromium-browser", "/snap/bin/chromium"],
                 [("chromium.exe", r"Chromium\Application\chrome.exe")], ["org.chromium.Chromium"]),
    "edge": ("Microsoft Edge", ["Microsoft Edge"], ["microsoft-edge", "microsoft-edge-stable"],
             [("msedge.exe", r"Microsoft\Edge\Application\msedge.exe")], ["com.microsoft.Edge"]),
    "brave": ("Brave", ["Brave Browser"], ["brave-browser", "brave-browser-stable", "brave", "/snap/bin/brave"],
              [("brave.exe", r"BraveSoftware\Brave-Browser\Application\brave.exe")], ["com.brave.Browser"]),
    "vivaldi": ("Vivaldi", ["Vivaldi"], ["vivaldi-stable", "vivaldi"],
                [("vivaldi.exe", r"Vivaldi\Application\vivaldi.exe")], ["com.vivaldi.Vivaldi"]),
    "firefox": ("Firefox", ["Firefox"], ["firefox", "/snap/bin/firefox"],
                [("firefox.exe", r"Mozilla Firefox\firefox.exe")], ["org.mozilla.firefox"]),
    "safari": ("Safari", ["Safari"], [], [], []),
}
# These can show a page as its own app window (--app), so they're tried first, in this order.
APP_WINDOW = ("chrome", "chromium", "edge", "brave", "vivaldi")
BROWSER_ENV = "NOTETOSELF_BROWSER"
CHOICES = set(BROWSERS) | {"auto", "default"}  # what Settings can save
FLATPAK_BINS = ["/var/lib/flatpak/exports/bin", "~/.local/share/flatpak/exports/bin"]


def _win_registry(exe: str) -> Optional[str]:
    try:
        import winreg  # type: ignore
    except ImportError:
        return None
    key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths" + "\\" + exe
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, key) as k:
                value, _ = winreg.QueryValueEx(k, "")
                if value:
                    return value.strip('"')
        except OSError:
            continue
    return None


def find_browser(names: Sequence[str] = APP_WINDOW, platform: Optional[str] = None,
                 exists: Callable[[str], bool] = os.path.exists,
                 which: Callable[[str], Optional[str]] = shutil.which,
                 registry: Callable[[str], Optional[str]] = _win_registry,
                 environ: Optional[dict] = None) -> Optional[Tuple[str, str, str]]:
    """(name, "mac"|"linux"|"windows", path) for the first of `names` installed here."""
    platform = platform or sys.platform
    env = os.environ if environ is None else environ
    home = env.get("HOME") or os.path.expanduser("~")
    for name in names:
        _, mac_apps, commands, win_exes, flatpaks = BROWSERS[name]
        if platform == "darwin":
            for base in ("/Applications", posixpath.join(home, "Applications")):
                for app in mac_apps:
                    p = posixpath.join(base, app + ".app")
                    if exists(p):
                        return name, "mac", p
        elif platform.startswith("win"):
            for exe, rel in win_exes:
                p = registry(exe)
                if p and exists(p):
                    return name, "windows", p
                for var in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
                    base = env.get(var)
                    if base and exists(ntpath.join(base, rel)):
                        return name, "windows", ntpath.join(base, rel)
        else:
            for cmd in commands:
                p = (cmd if exists(cmd) else None) if cmd.startswith("/") else which(cmd)
                if p:
                    return name, "linux", p
            for app in flatpaks:
                for d in FLATPAK_BINS:
                    p = posixpath.join(d.replace("~", home, 1), app)
                    if exists(p):
                        return name, "linux", p
    return None


def browser_at(path: str, platform: Optional[str] = None) -> Tuple[str, str, str]:
    """(name, kind, path) for a browser given by its path (--browser /path/to/browser)."""
    platform = platform or sys.platform
    kind = "mac" if platform == "darwin" else "windows" if platform.startswith("win") else "linux"
    base = os.path.basename(path.rstrip("/\\")).lower()
    name = "path"
    for known, hint in (("chrome", "chrome"), ("chromium", "chromium"), ("edge", "edge"),
                        ("brave", "brave"), ("vivaldi", "vivaldi"), ("firefox", "firefox"),
                        ("safari", "safari")):
        if hint in base:
            name = known
            break
    return name, kind, path


def has_desktop(platform: Optional[str] = None, environ: Optional[dict] = None) -> bool:
    """False on Linux without a graphical session (e.g. logged in over SSH)."""
    platform = platform or sys.platform
    env = os.environ if environ is None else environ
    if platform == "darwin" or platform.startswith("win"):
        return True
    return bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


def browser_command(name: str, kind: str, path: str, url: str) -> List[str]:
    if name in APP_WINDOW:  # its own window, without tabs or an address bar
        return (["open", "-na", path, "--args", "--app=" + url] if kind == "mac"
                else [path, "--app=" + url])
    if kind == "mac":  # Firefox, Safari: hand the page to the running browser
        return ["open", "-a", path, url]
    if name == "firefox":
        return [path, "--new-window", url]
    return [path, url]


def choose_browser(choice: Optional[str], warn: Callable[[str], None] = print
                   ) -> Optional[Tuple[str, str, str]]:
    """The browser for `choice` (a name, "auto", "default", or a path); None means the
    system's default browser."""
    choice = (choice or "auto").strip()
    key = choice.lower()
    if key == "default":
        return None
    if key in BROWSERS:
        found = find_browser((key,))
        if not found:
            warn("%s isn't installed, so opening your default browser instead." % BROWSERS[key][0])
        return found
    if key != "auto":
        if os.path.exists(choice):
            return browser_at(choice)
        warn("Unknown browser %r; see notetoself --help. Opening the usual one instead." % choice)
    return find_browser(APP_WINDOW)


def installed() -> List[Dict[str, Any]]:
    """The browsers installed here, for Settings: [{id, name, appWindow}]."""
    out = []
    for name in BROWSERS:
        if find_browser((name,)):
            out.append({"id": name, "name": BROWSERS[name][0], "appWindow": name in APP_WINDOW})
    return out


def open_window(url: str, choice: Optional[str] = None, warn: Callable[[str], None] = print) -> bool:
    """Open the app window in the chosen or best available browser. False if there's no
    desktop to show it on."""
    if not has_desktop():
        return False  # webbrowser would pick a text browser (lynx, w3m) and take over the terminal
    found = choose_browser(choice, warn)
    if found:
        name, kind, path = found
        cmd = browser_command(name, kind, path, url)
        kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                        "stderr": subprocess.DEVNULL}
        if kind == "windows":
            kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_PROCESS_GROUP
        elif kind == "linux":
            kwargs["start_new_session"] = True
        try:
            subprocess.Popen(cmd, **kwargs)
            return True
        except OSError:
            pass
    webbrowser.open(url)
    return True
