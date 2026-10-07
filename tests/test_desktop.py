"""Adding Note to Self to your apps: the macOS app bundle and the Linux menu entry."""

from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import tempfile
import unittest
from unittest import mock

from nts import __version__, desktop


class DesktopCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="nts-home-")
        self.addCleanup(shutil.rmtree, self.home, True)
        patcher = mock.patch.object(desktop, "launcher", return_value="/opt/homebrew/bin/notetoself")
        patcher.start()
        self.addCleanup(patcher.stop)


class TestMac(DesktopCase):
    def install(self, extra=()):
        with mock.patch.object(desktop.subprocess, "run"):  # no lsregister in tests
            return desktop.install(list(extra), platform="darwin", home=self.home)

    def test_app_bundle(self):
        app = self.install(["--dir", "/Users/u/My Notes"])
        self.assertEqual(app, os.path.join(self.home, "Applications", "Note to Self.app"))
        with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
            info = plistlib.load(f)
        self.assertEqual(info["CFBundleIdentifier"], desktop.BUNDLE_ID)
        self.assertEqual(info["CFBundleName"], "Note to Self")
        self.assertEqual(info["CFBundleShortVersionString"], __version__)
        self.assertEqual(info["CFBundleExecutable"], "note-to-self")
        self.assertTrue(info["LSUIElement"])
        script = os.path.join(app, "Contents", "MacOS", "note-to-self")
        with open(script, encoding="utf-8") as f:
            last = f.read().strip().splitlines()[-1]
        self.assertEqual(shlex.split(last), ["exec", "/opt/homebrew/bin/notetoself", "--background",
                                             "--dir", "/Users/u/My Notes"])
        if os.name != "nt":
            self.assertTrue(os.access(script, os.X_OK))
        self.assertGreater(os.path.getsize(os.path.join(app, "Contents", "Resources", "AppIcon.icns")), 1000)

    def test_reinstall_and_uninstall(self):
        self.install()
        app = self.install(["--port", "6690"])  # replaces our own app
        with open(os.path.join(app, "Contents", "MacOS", "note-to-self"), encoding="utf-8") as f:
            self.assertIn("--port 6690", f.read())
        self.assertEqual(desktop.uninstall(platform="darwin", home=self.home), [app])
        self.assertFalse(os.path.exists(app))
        self.assertEqual(desktop.uninstall(platform="darwin", home=self.home), [])

    def test_never_replaces_someone_elses_app(self):
        app = os.path.join(self.home, "Applications", "Note to Self.app", "Contents")
        os.makedirs(app)
        with open(os.path.join(app, "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleIdentifier": "com.example.other"}, f)
        with self.assertRaises(desktop.DesktopError):
            self.install()
        self.assertEqual(desktop.uninstall(platform="darwin", home=self.home), [])
        self.assertTrue(os.path.exists(os.path.join(app, "Info.plist")))


class TestLinux(DesktopCase):
    def test_menu_entry_and_icon(self):
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": ""}), \
                mock.patch.object(desktop.shutil, "which", return_value=None):
            entry = desktop.install(["--dir", "/home/u/My Notes"], platform="linux", home=self.home)
        self.assertEqual(entry, os.path.join(self.home, ".local", "share", "applications", "note-to-self.desktop"))
        with open(entry, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertIn('Exec=/opt/homebrew/bin/notetoself --background --dir "/home/u/My Notes"', lines)
        for line in ("[Desktop Entry]", "Name=Note to Self", "Icon=note-to-self", "Terminal=false"):
            self.assertIn(line, lines)
        icon = os.path.join(self.home, ".local", "share", "icons", "hicolor", "scalable", "apps", "note-to-self.svg")
        self.assertTrue(os.path.isfile(icon))
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": ""}):
            self.assertEqual(sorted(desktop.uninstall(platform="linux", home=self.home)), sorted([entry, icon]))
        self.assertFalse(os.path.exists(entry))

    def test_exec_quoting(self):
        self.assertEqual(desktop._desktop_quote("plain"), "plain")
        self.assertEqual(desktop._desktop_quote("a b"), '"a b"')
        self.assertEqual(desktop._desktop_quote('say "hi" $HOME'), '"say \\"hi\\" \\$HOME"')
        self.assertEqual(desktop._desktop_quote("100%"), '"100%%"')

    def test_windows_isnt_supported_yet(self):
        with self.assertRaises(desktop.DesktopError):
            desktop.install([], platform="win32", home=self.home)


class TestLauncherPath(unittest.TestCase):
    def test_paths_that_survive_upgrades(self):
        with mock.patch.dict(os.environ, {"NOTETOSELF_LAUNCHER": ""}):
            self.assertEqual(desktop.launcher("/opt/homebrew/Cellar/note-to-self/0.1.0/libexec"),
                             "/opt/homebrew/bin/notetoself")
            self.assertEqual(desktop.launcher("/usr/lib/note-to-self"), "/usr/bin/notetoself")
            if os.name != "nt":
                self.assertEqual(desktop.launcher("/home/u/src/note-to-self"),
                                 "/home/u/src/note-to-self/notetoself")
        with mock.patch.dict(os.environ, {"NOTETOSELF_LAUNCHER": "/x/notetoself"}):
            self.assertEqual(desktop.launcher("/anything"), "/x/notetoself")


if __name__ == "__main__":
    unittest.main()
