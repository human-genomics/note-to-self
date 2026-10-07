"""Put Note to Self with your other apps, so you can open it by clicking it:

- macOS: ~/Applications/Note to Self.app, which shows up in Launchpad and Spotlight and can
  be dragged to the Dock. It's made on this Mac, so macOS opens it without any warnings.
- Linux: an entry in the applications menu (~/.local/share/applications), with its icon.

    notetoself --install-app      (and --uninstall-app)

The app runs this same `notetoself` in the background, so closing the window doesn't stop
it; Quit in the window's ☰ menu (or `notetoself --stop`) does.
"""

from __future__ import annotations

import os
import plistlib
import posixpath
import shlex
import shutil
import subprocess
import sys
from typing import List, Optional

from . import __version__

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # has notetoself.py and web/
ICONS = os.path.join(ROOT, "web", "icons")
APP_NAME = "Note to Self"
BUNDLE_ID = "io.github.human-genomics.note-to-self"
DESKTOP_ID = "note-to-self"
LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
              "LaunchServices.framework/Support/lsregister")


class DesktopError(Exception):
    pass


def launcher(root: str = ROOT) -> str:
    """The `notetoself` command, by a path that stays valid across upgrades."""
    if os.environ.get("NOTETOSELF_LAUNCHER"):
        return os.environ["NOTETOSELF_LAUNCHER"]
    if "/Cellar/" in root:  # Homebrew: <prefix>/Cellar/note-to-self/<version>/libexec
        return posixpath.join(root.split("/Cellar/")[0], "bin", "notetoself")
    if root == "/usr/lib/note-to-self":  # the .deb
        return "/usr/bin/notetoself"
    return os.path.join(root, "notetoself.bat" if os.name == "nt" else "notetoself")


def _desktop_quote(arg: str) -> str:
    """Quote one Exec= argument (Desktop Entry Specification)."""
    if arg and not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`%'):
        return arg
    return '"%s"' % "".join("\\" + c if c in '"`$\\' else c for c in arg).replace("%", "%%")


def desktop_entry(exec_args: List[str]) -> str:
    """The launcher entry for the applications menu (also used by the .deb)."""
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name=" + APP_NAME,
        "GenericName=Notes",
        "Comment=Chat with yourself; every note is saved as Markdown on your computer",
        "Exec=" + " ".join(_desktop_quote(a) for a in exec_args),
        "TryExec=" + exec_args[0],  # menus hide the entry once the command is gone
        "Icon=" + DESKTOP_ID,
        "Terminal=false",
        "Categories=Office;",
        "Keywords=notes;markdown;journal;chat;signal;",
        "StartupNotify=false",
        "",
    ])


def install(extra: List[str], platform: Optional[str] = None, home: Optional[str] = None) -> str:
    """Add the app (opening with `extra` arguments, e.g. --dir); returns where it went."""
    platform = platform or sys.platform
    home = home or os.path.expanduser("~")
    command = [launcher(), "--background"] + list(extra)
    if platform == "darwin":
        return _install_mac(command, home)
    if platform.startswith("win"):
        raise DesktopError("Adding Note to Self to your apps isn't supported on Windows yet.")
    return _install_linux(command, home)


def uninstall(platform: Optional[str] = None, home: Optional[str] = None) -> List[str]:
    """Remove what install() added; returns the paths removed."""
    platform = platform or sys.platform
    home = home or os.path.expanduser("~")
    removed = []
    if platform == "darwin":
        app = _mac_app(home)
        if os.path.isdir(app) and _is_ours(app):
            shutil.rmtree(app)
            removed.append(app)
    elif not platform.startswith("win"):
        for path in (_linux_paths(home)):
            if os.path.exists(path):
                os.remove(path)
                removed.append(path)
    return removed


# -- macOS ----------------------------------------------------------------------

# The app's executable. Package managers can't remove an app they didn't install, so if
# Note to Self has been uninstalled, the app removes itself instead of being a dead icon.
MAC_SCRIPT = """#!/bin/sh
# Opens Note to Self, starting it in the background first if needed.
if [ ! -x {exe} ]; then
  app="$(cd "$(dirname "$0")/../.." && pwd -P)"
  case "$app" in */"Note to Self.app") rm -rf "$app" ;; esac
  osascript -e 'display notification "Note to Self has been uninstalled, so this app was removed too. Your notes are untouched." with title "Note to Self"' >/dev/null 2>&1
  exit 0
fi
exec {command}
"""

def _mac_app(home: str) -> str:
    return os.path.join(home, "Applications", APP_NAME + ".app")


def _is_ours(app: str) -> bool:
    try:
        with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
            return plistlib.load(f).get("CFBundleIdentifier") == BUNDLE_ID
    except (OSError, ValueError, plistlib.InvalidFileException):
        return False


def _install_mac(command: List[str], home: str) -> str:
    app = _mac_app(home)
    if os.path.exists(app):
        if not _is_ours(app):
            raise DesktopError("%s already exists and isn't Note to Self's; move it first." % app)
        shutil.rmtree(app)
    contents = os.path.join(app, "Contents")
    os.makedirs(os.path.join(contents, "MacOS"))
    os.makedirs(os.path.join(contents, "Resources"))
    info = {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleVersion": __version__,
        "CFBundleShortVersionString": __version__,
        "CFBundlePackageType": "APPL",
        "CFBundleExecutable": "note-to-self",
        "CFBundleIconFile": "AppIcon",
        "LSUIElement": True,  # no Dock icon of its own: the window is the app
        "LSMinimumSystemVersion": "10.13",
        "NSHighResolutionCapable": True,
    }
    with open(os.path.join(contents, "Info.plist"), "wb") as f:
        plistlib.dump(info, f)
    script = os.path.join(contents, "MacOS", "note-to-self")
    with open(script, "w", encoding="utf-8") as f:
        f.write(MAC_SCRIPT.format(exe=shlex.quote(command[0]),
                                  command=" ".join(shlex.quote(a) for a in command)))
    os.chmod(script, 0o755)
    shutil.copyfile(os.path.join(ICONS, "app.icns"), os.path.join(contents, "Resources", "AppIcon.icns"))
    if os.path.exists(LSREGISTER):  # let Launchpad and Spotlight know right away
        subprocess.run([LSREGISTER, "-f", app], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return app


# -- Linux (freedesktop) --------------------------------------------------------

def _linux_paths(home: str) -> List[str]:
    data = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    return [os.path.join(data, "applications", DESKTOP_ID + ".desktop"),
            os.path.join(data, "icons", "hicolor", "scalable", "apps", DESKTOP_ID + ".svg")]


def _install_linux(command: List[str], home: str) -> str:
    entry, icon = _linux_paths(home)
    os.makedirs(os.path.dirname(entry), exist_ok=True)
    os.makedirs(os.path.dirname(icon), exist_ok=True)
    shutil.copyfile(os.path.join(ICONS, "app.svg"), icon)
    with open(entry, "w", encoding="utf-8") as f:
        f.write(desktop_entry(command))
    os.chmod(entry, 0o644)
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", os.path.dirname(entry)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return entry
