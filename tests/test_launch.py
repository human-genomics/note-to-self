"""Launcher: browser discovery, single-instance probing, and main() outcomes."""

from __future__ import annotations

import io
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from nts import launch
from nts.server import Server
from nts.store import Store, folder_key


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestArguments(unittest.TestCase):
    def test_browser_and_port_arguments(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                launch.parse_args(["--browser", "netscape"])
            with self.assertRaises(SystemExit):
                launch.parse_args(["--port", "6697"])  # browsers refuse this one
        self.assertEqual(launch.parse_args(["--browser", "firefox"]).browser, "firefox")
        self.assertEqual(launch.parse_args(["--browser", "default"]).browser, "default")

    def test_browser_choice_precedence(self):
        tmp = tempfile.mkdtemp(prefix="nts-choice-")
        self.addCleanup(shutil.rmtree, tmp, True)
        store = Store(tmp, warm=False)
        store.set_settings({"browser": "safari"})
        with mock.patch.dict(os.environ, {"NOTETOSELF_BROWSER": ""}):
            self.assertEqual(launch.browser_choice(None, tmp), "safari")  # Settings
            self.assertEqual(launch.browser_choice("firefox", tmp), "firefox")  # --browser wins
        with mock.patch.dict(os.environ, {"NOTETOSELF_BROWSER": "edge"}):
            self.assertEqual(launch.browser_choice(None, tmp), "edge")  # the environment beats Settings
            self.assertEqual(launch.browser_choice("chrome", tmp), "chrome")


class Foreign(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"hello"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class TestProbeAndMain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nts-launch-")
        self.a = os.path.join(self.tmp, "a")
        self.b = os.path.join(self.tmp, "b")
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.shutdown()
            s.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def serve(self, srv):
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.servers.append(srv)
        return srv.server_address[1]

    def run_main(self, argv, terminal=True):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
                mock.patch.object(launch, "interactive", return_value=terminal):
            code = launch.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_key_stays_out_of_log_files(self):
        # Under `brew services` (or `> log`), stdout isn't a terminal: don't print the key.
        with mock.patch.object(launch.Server, "serve_forever", side_effect=KeyboardInterrupt), \
                mock.patch.object(launch, "open_window", return_value=False) as ow:
            code, out, _ = self.run_main(["--dir", self.a, "--port", str(free_port())], terminal=False)
        self.assertEqual(code, 0)
        key = folder_key(self.a)
        self.assertNotIn(key, out)
        self.assertIn("Run notetoself to open it.", out)
        self.assertNotIn("No desktop session", out)
        self.assertIn(key, ow.call_args[0][0])  # the window itself still gets the key

    def test_probe(self):
        self.assertEqual(launch.probe(free_port()), ("free", None))
        store = Store(self.a, warm=False)
        port = self.serve(Server(0, store))
        self.assertEqual(launch.probe(port, store.key), ("ours", self.a))
        # Without the folder's key (another folder, or another user) the folder stays private.
        self.assertEqual(launch.probe(port), ("ours", None))
        fport = self.serve(HTTPServer(("127.0.0.1", 0), Foreign))
        self.assertEqual(launch.probe(fport), ("foreign", None))

    def test_already_running_same_and_other_dir(self):
        store = Store(self.a, warm=False)
        port = self.serve(Server(0, store))
        with mock.patch.object(launch, "open_window") as ow:
            code, out, _ = self.run_main(["--dir", self.a, "--port", str(port)])
        self.assertEqual(code, 0)
        self.assertIn("already running", out)
        ow.assert_called_once_with("http://127.0.0.1:%d/?key=%s" % (port, store.key), None)
        with mock.patch.object(launch, "open_window") as ow:
            code, _, err = self.run_main(["--dir", self.b, "--port", str(port)])
        self.assertEqual(code, 1)
        self.assertIn("with a different notes folder", err)
        ow.assert_not_called()

    def test_foreign_port(self):
        fport = self.serve(HTTPServer(("127.0.0.1", 0), Foreign))
        with mock.patch.object(launch.time, "sleep"), mock.patch.object(launch, "open_window") as ow:
            code, _, err = self.run_main(["--dir", self.a, "--port", str(fport)])
        self.assertEqual(code, 1)
        self.assertIn("used by another program; try --port %d" % (fport + 1), err)
        ow.assert_not_called()
        self.assertFalse(os.path.exists(self.a))  # no folder created when we can't start

    def test_normal_start_and_ctrl_c(self):
        port = free_port()
        with mock.patch.object(launch.Server, "serve_forever", side_effect=KeyboardInterrupt), \
                mock.patch.object(launch, "open_window") as ow:
            code, out, _ = self.run_main(["--dir", self.a, "--port", str(port)])
        self.assertEqual(code, 0)
        key = folder_key(self.a)
        self.assertIn("Your notes are saved in %s" % self.a, out)
        self.assertIn("Open it at http://127.0.0.1:%d/?key=%s" % (port, key), out)
        self.assertIn("Stopped.", out)
        ow.assert_called_once_with("http://127.0.0.1:%d/?key=%s" % (port, key), None)
        self.assertTrue(os.path.isdir(os.path.join(self.a, "Note to Self")))
        self.assertNotIn("No desktop session", out)
        with mock.patch.object(launch.Server, "serve_forever", side_effect=KeyboardInterrupt), \
                mock.patch.object(launch, "open_window") as ow:
            self.run_main(["--dir", self.a, "--port", str(free_port()), "--no-open"])
        ow.assert_not_called()

    def test_refuses_a_folder_with_other_files(self):
        os.makedirs(self.b)
        with open(os.path.join(self.b, "report.docx"), "w") as f:
            f.write("not ours")
        with mock.patch.object(launch, "open_window") as ow:
            code, _, err = self.run_main(["--dir", self.b, "--port", str(free_port())])
        self.assertEqual(code, 1)
        self.assertIn("already has other files in it", err)
        ow.assert_not_called()
        self.assertEqual(os.listdir(self.b), ["report.docx"])

    def test_stop(self):
        code, out, _ = self.run_main(["--dir", self.a, "--port", str(free_port()), "--stop"])
        self.assertEqual((code, out.strip()), (0, "Note to Self isn't running."))
        store = Store(self.a, warm=False)
        srv = Server(0, store)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        t.start()
        try:
            code, _, err = self.run_main(["--dir", self.b, "--port", str(port), "--stop"])
            self.assertEqual(code, 1)  # someone else's: leave it alone
            self.assertIn("leaving it alone", err)
            self.assertTrue(t.is_alive())
            code, out, _ = self.run_main(["--dir", self.a, "--port", str(port), "--stop"])
            self.assertEqual(code, 0)
            self.assertIn("Stopped Note to Self.", out)
            t.join(5)
            self.assertFalse(t.is_alive())
        finally:
            if t.is_alive():
                srv.shutdown()
            srv.server_close()

    @unittest.skipUnless(hasattr(os, "fork"), "POSIX only")
    def test_background_detaches_and_keeps_serving(self):
        port = free_port()
        notetoself = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "notetoself.py")
        started = time.time()
        done = subprocess.run([sys.executable, notetoself, "--background", "--no-open", "--port", str(port),
                               "--dir", self.a], timeout=30)
        self.assertEqual(done.returncode, 0)
        self.assertLess(time.time() - started, 15)  # it returned; the server lives on
        for _ in range(100):
            if launch.probe(port)[0] == "ours":
                break
            time.sleep(0.05)
        self.assertEqual(launch.probe(port, folder_key(self.a)), ("ours", self.a))
        code, out, _ = self.run_main(["--dir", self.a, "--port", str(port), "--stop"])
        self.assertEqual(code, 0)

    def test_install_app(self):
        with mock.patch.object(launch.desktop, "install", return_value="/Users/u/Applications/Note to Self.app") as inst, \
                mock.patch.object(launch.subprocess, "run"):
            code, out, _ = self.run_main(["--install-app", "--dir", self.a, "--port", "6690"])
        self.assertEqual(code, 0)
        self.assertEqual(inst.call_args[0][0], ["--dir", self.a, "--port", "6690"])
        self.assertIn("Added Note to Self to your apps", out)
        with mock.patch.object(launch.desktop, "install", return_value="x") as inst, \
                mock.patch.object(launch.subprocess, "run"):
            self.run_main(["--install-app"])
        self.assertEqual(inst.call_args[0][0], [])  # the default folder and port need no arguments

    def test_says_so_after_delete_all(self):
        def wiped(server, poll_interval=0.5):
            server.wiped = True  # what "Delete all data" does before stopping the server

        with mock.patch.object(launch.Server, "serve_forever", autospec=True, side_effect=wiped), \
                mock.patch.object(launch, "open_window"):
            code, out, _ = self.run_main(["--dir", self.a, "--port", str(free_port())])
        self.assertEqual(code, 0)
        self.assertIn("All data was deleted", out)

    def test_start_without_desktop_prints_ssh_hint(self):
        port = free_port()
        with mock.patch.object(launch.Server, "serve_forever", side_effect=KeyboardInterrupt), \
                mock.patch.object(launch, "open_window", return_value=False):
            code, out, _ = self.run_main(["--dir", self.a, "--port", str(port)])
        self.assertEqual(code, 0)
        self.assertIn("No desktop session", out)
        self.assertIn("ssh -L %d:127.0.0.1:%d" % (port, port), out)

    def test_bad_port(self):
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                launch.parse_args(["--port", "70000"])


if __name__ == "__main__":
    unittest.main()
