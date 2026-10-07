"""Finding browsers and opening the app window in the chosen one."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from nts import browsers


class TestBrowsers(unittest.TestCase):
    URL = "http://127.0.0.1:1/?key=k"

    def test_mac(self):
        want = "/Users/u/Applications/Brave Browser.app"
        found = browsers.find_browser(platform="darwin", exists=lambda p: p == want, environ={"HOME": "/Users/u"})
        self.assertEqual(found, ("brave", "mac", want))
        self.assertEqual(browsers.browser_command("brave", "mac", want, self.URL),
                         ["open", "-na", want, "--args", "--app=" + self.URL])
        self.assertIsNone(browsers.find_browser(platform="darwin", exists=lambda p: False))

    def test_linux(self):
        found = browsers.find_browser(platform="linux", which=lambda n: "/usr/bin/" + n if n == "chromium" else None,
                                    exists=lambda p: False)
        self.assertEqual(found, ("chromium", "linux", "/usr/bin/chromium"))
        self.assertEqual(browsers.browser_command("chromium", "linux", "/usr/bin/chromium", "U"),
                         ["/usr/bin/chromium", "--app=U"])
        self.assertIsNone(browsers.find_browser(platform="linux", which=lambda n: None, exists=lambda p: False))

    def test_linux_snap_and_flatpak(self):
        none = lambda n: None  # noqa: E731  nothing on PATH
        found = browsers.find_browser(platform="linux", which=none, exists=lambda p: p == "/snap/bin/chromium")
        self.assertEqual(found, ("chromium", "linux", "/snap/bin/chromium"))
        want = "/home/u/.local/share/flatpak/exports/bin/com.google.Chrome"
        found = browsers.find_browser(platform="linux", which=none, exists=lambda p: p == want,
                                    environ={"HOME": "/home/u"})
        self.assertEqual(found, ("chrome", "linux", want))
        # Chrome on PATH wins over everything else.
        found = browsers.find_browser(platform="linux", exists=lambda p: True,
                                    which=lambda n: "/usr/bin/" + n if n == "google-chrome" else None)
        self.assertEqual(found, ("chrome", "linux", "/usr/bin/google-chrome"))

    def test_windows(self):
        reg = {"msedge.exe": r"C:\Edge\msedge.exe"}
        found = browsers.find_browser(platform="win32", exists=lambda p: p == r"C:\Edge\msedge.exe",
                                    registry=reg.get, environ={})
        self.assertEqual(found, ("edge", "windows", r"C:\Edge\msedge.exe"))
        want = os.path.join(r"C:\PF", r"Google\Chrome\Application\chrome.exe")
        found = browsers.find_browser(platform="win32", exists=lambda p: p == want, registry=lambda e: None,
                                    environ={"ProgramFiles": r"C:\PF"})
        self.assertEqual(found, ("chrome", "windows", want))

    def test_firefox_and_safari(self):
        found = browsers.find_browser(("firefox",), platform="linux", exists=lambda p: False,
                                    which=lambda n: "/usr/bin/firefox" if n == "firefox" else None)
        self.assertEqual(found, ("firefox", "linux", "/usr/bin/firefox"))
        self.assertEqual(browsers.browser_command("firefox", "linux", "/usr/bin/firefox", self.URL),
                         ["/usr/bin/firefox", "--new-window", self.URL])
        # On macOS, Firefox and Safari get the page handed to the running browser.
        self.assertEqual(browsers.browser_command("firefox", "mac", "/Applications/Firefox.app", self.URL),
                         ["open", "-a", "/Applications/Firefox.app", self.URL])
        found = browsers.find_browser(("safari",), platform="darwin",
                                    exists=lambda p: p == "/Applications/Safari.app", environ={"HOME": "/Users/u"})
        self.assertEqual(found, ("safari", "mac", "/Applications/Safari.app"))
        self.assertIsNone(browsers.find_browser(("safari",), platform="linux", which=lambda n: "/x", exists=lambda p: True))

    def test_choose_browser(self):
        with mock.patch.object(browsers, "find_browser", return_value=("firefox", "linux", "/f")) as fb:
            self.assertEqual(browsers.choose_browser("Firefox"), ("firefox", "linux", "/f"))
        fb.assert_called_once_with(("firefox",))
        self.assertIsNone(browsers.choose_browser("default"))  # the system's default browser
        said = []
        with mock.patch.object(browsers, "find_browser", return_value=None):
            self.assertIsNone(browsers.choose_browser("brave", said.append))  # not installed
        self.assertIn("Brave isn't installed", said[0])
        with mock.patch.object(browsers, "find_browser", return_value=None) as fb:
            browsers.choose_browser(None)
        fb.assert_called_once_with(browsers.APP_WINDOW)  # the usual: an app window if possible
        self.assertEqual(browsers.browser_at("/opt/brave.com/brave/brave-browser", "linux"),
                         ("brave", "linux", "/opt/brave.com/brave/brave-browser"))
        self.assertEqual(browsers.browser_at("/usr/local/bin/mybrowser", "linux")[0], "path")

    def test_has_desktop(self):
        self.assertFalse(browsers.has_desktop("linux", {}))
        self.assertTrue(browsers.has_desktop("linux", {"DISPLAY": ":0"}))
        self.assertTrue(browsers.has_desktop("linux", {"WAYLAND_DISPLAY": "wayland-0"}))
        self.assertTrue(browsers.has_desktop("darwin", {}))
        self.assertTrue(browsers.has_desktop("win32", {}))

    def test_open_window_fallback(self):
        with mock.patch.object(browsers, "has_desktop", return_value=True), \
                mock.patch.object(browsers, "choose_browser", return_value=None), \
                mock.patch.object(browsers.webbrowser, "open") as wb:
            self.assertTrue(browsers.open_window("http://127.0.0.1:9/"))
        wb.assert_called_once_with("http://127.0.0.1:9/")
        with mock.patch.object(browsers, "has_desktop", return_value=True), \
                mock.patch.object(browsers, "choose_browser", return_value=("chrome", "linux", "/x/chrome")), \
                mock.patch.object(browsers.subprocess, "Popen") as popen:
            self.assertTrue(browsers.open_window("http://127.0.0.1:9/"))
        self.assertEqual(popen.call_args[0][0], ["/x/chrome", "--app=http://127.0.0.1:9/"])

    def test_open_window_without_desktop(self):
        # Over SSH, webbrowser would run lynx/w3m in the terminal; open nothing instead.
        with mock.patch.object(browsers, "has_desktop", return_value=False), \
                mock.patch.object(browsers.subprocess, "Popen") as popen, \
                mock.patch.object(browsers.webbrowser, "open") as wb:
            self.assertFalse(browsers.open_window("http://127.0.0.1:9/"))
        popen.assert_not_called()
        wb.assert_not_called()


    def test_installed_lists_what_is_there(self):
        def find(names, **kw):
            return (names[0], "linux", "/usr/bin/x") if names[0] in ("chrome", "firefox") else None
        with mock.patch.object(browsers, "find_browser", side_effect=find):
            found = browsers.installed()
        self.assertEqual(found, [{"id": "chrome", "name": "Google Chrome", "appWindow": True},
                                 {"id": "firefox", "name": "Firefox", "appWindow": False}])


if __name__ == "__main__":
    unittest.main()
