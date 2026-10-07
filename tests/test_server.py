"""HTTP layer against a real server on an ephemeral port: security, MIME, ranges, API flow."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from urllib.parse import quote

from unittest import mock

from nts import browsers
from nts.server import Server
from nts.store import Store

from tests.test_store import png_bytes


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="nts-srv-")
        cls.web = os.path.join(cls.tmp, "web")
        os.makedirs(os.path.join(cls.web, "js"))
        with open(os.path.join(cls.web, "index.html"), "w") as f:
            f.write("<!doctype html><title>t</title>")
        with open(os.path.join(cls.web, "js", "main.js"), "w") as f:
            f.write("export const x = 1;\n")
        with open(os.path.join(cls.web, ".secret"), "w") as f:
            f.write("no")
        cls.root = os.path.join(cls.tmp, "notes")
        cls.store = Store(cls.root, warm=False)
        cls.srv = Server(0, cls.store, web_dir=cls.web)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, kwargs={"poll_interval": 0.05},
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def request(self, method, path, body=None, headers=None, api=True, host=None, raw=False,
                key=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host or "127.0.0.1:%d" % self.port}
        if api:
            h["X-NoteToSelf"] = "1"
        if key:  # what the browser sends after opening the launcher's link
            h["Cookie"] = "nts-%d=%s" % (self.port, self.store.key)
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in h.items():
            conn.putheader(k, v)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders()
        if body is not None:
            conn.send(body)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        if raw:
            return resp, data
        try:
            return resp, json.loads(data) if data else None
        except ValueError:
            return resp, data


class TestSecurity(ServerCase):
    def test_host_header(self):
        r, b = self.request("GET", "/api/ping", host="evil.example:%d" % self.port)
        self.assertEqual(r.status, 403)
        r, b = self.request("GET", "/api/ping", host="localhost:%d" % self.port)
        self.assertEqual(r.status, 200)
        r, b = self.request("GET", "/", api=False, host="127.0.0.1:1")
        self.assertEqual(r.status, 403)

    def test_api_header_and_fetch_site(self):
        r, b = self.request("GET", "/api/ping", api=False)
        self.assertEqual((r.status, b["error"]["code"]), (403, "forbidden"))
        r, b = self.request("POST", "/api/chats", {"name": "x"}, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status, 403)
        r, b = self.request("GET", "/api/ping", headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(r.status, 200)

    def test_no_options(self):
        r, _ = self.request("OPTIONS", "/api/chats", api=False)
        self.assertEqual(r.status, 501)

    def test_traversal(self):
        for path in ("/..%2F..%2Fetc%2Fpasswd", "/js/..%2F..%2Fsecret", "/%5Cwindows",
                     "/index.html%00", "/files/Note%20to%20Self/attachments/..%2F2026-10-04.md",
                     "/files/Note%20to%20Self/attachments/%2E%2E", "/.secret", "/%FF"):
            r, _ = self.request("GET", path, api=False)
            self.assertIn(r.status, (400, 404), path)

    def test_security_headers(self):
        r, _ = self.request("GET", "/", api=False, raw=True)
        self.assertEqual(r.status, 200)
        self.assertIn("script-src 'self'", r.getheader("Content-Security-Policy"))
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(r.getheader("Cross-Origin-Resource-Policy"), "same-origin")
        r, _ = self.request("GET", "/api/chats")
        self.assertEqual(r.getheader("Cache-Control"), "no-store")


class TestStatic(ServerCase):
    def test_mime_and_etag(self):
        r, data = self.request("GET", "/js/main.js", api=False, raw=True)
        self.assertEqual(r.getheader("Content-Type"), "text/javascript; charset=utf-8")
        etag = r.getheader("ETag")
        r, _ = self.request("GET", "/js/main.js", api=False, raw=True, headers={"If-None-Match": etag})
        self.assertEqual(r.status, 304)
        r, data = self.request("HEAD", "/index.html", api=False, raw=True)
        self.assertEqual((r.status, data), (200, b""))
        r, _ = self.request("GET", "/nope.js", api=False)
        self.assertEqual(r.status, 404)


class TestFlow(ServerCase):
    def test_full_flow(self):
        r, b = self.request("GET", "/api/ping")
        self.assertEqual(b["app"], "note-to-self")
        self.assertEqual(b["defaultChat"], "Note to Self")
        r, b = self.request("POST", "/api/chats", {"name": "Flow Chat"})
        self.assertEqual(r.status, 201)
        chat = quote("Flow Chat", safe="")
        data = png_bytes(4, 3)
        r, att = self.request("POST", "/api/chats/%s/attachments?name=%s" % (chat, quote("my pic.png")),
                              data, headers={"Content-Type": "image/png"})
        self.assertEqual((r.status, att["kind"], att["w"], att["h"]), (201, "image", 4, 3))
        r, sent = self.request("POST", "/api/chats/%s/messages" % chat,
                               {"text": "hello world", "attachments": [{"file": att["file"], "name": att["name"]}]})
        self.assertEqual(r.status, 201)
        msg = sent["message"]
        self.assertEqual(msg["attachments"][0]["name"], "my pic.png")
        r, page = self.request("GET", "/api/chats/%s/messages" % chat)
        self.assertEqual(page["days"][0]["messages"][0]["id"], msg["id"])
        self.assertEqual(page["rev"], sent["rev"]["after"])
        r, ed = self.request("PATCH", "/api/chats/%s/messages/%s" % (chat, msg["id"]),
                             {"text": "hello edited", "hash": msg["hash"]})
        self.assertEqual((r.status, ed["message"]["edited"]), (200, True))
        r, b = self.request("PATCH", "/api/chats/%s/messages/%s" % (chat, msg["id"]),
                            {"text": "stale", "hash": msg["hash"]})
        self.assertEqual((r.status, b["error"]["code"]), (409, "conflict"))
        self.assertIn("days", b)
        r, s = self.request("GET", "/api/search?q=%s&chat=%s" % ("hel", chat))
        self.assertEqual(s["messages"][0]["id"], msg["id"])
        # attachment download
        url = msg["attachments"][0]["url"]
        r, body = self.request("GET", url, api=False, raw=True)
        self.assertEqual((r.status, body), (200, data))
        self.assertEqual(r.getheader("Content-Type"), "image/png")
        self.assertTrue(r.getheader("Content-Disposition").startswith("inline"))
        self.assertTrue(r.getheader("Content-Security-Policy").startswith("sandbox"))
        r, body = self.request("GET", url + "?download=1", api=False, raw=True)
        self.assertTrue(r.getheader("Content-Disposition").startswith("attachment"))
        new = ed["message"]
        r, dl = self.request("POST", "/api/chats/%s/messages/delete" % chat,
                             {"items": [{"id": new["id"], "hash": new["hash"]}]})
        self.assertEqual((r.status, dl["deleted"]), (200, 1))
        r, b = self.request("DELETE", "/api/chats/%s" % chat)
        self.assertEqual((r.status, b), (200, {"ok": True}))

    def test_ranges_and_svg_download(self):
        chat = quote("Note to Self", safe="")
        payload = bytes(range(100))
        r, att = self.request("POST", "/api/chats/%s/attachments?name=clip.mp4" % chat, payload)
        url = "/files/%s/attachments/%s" % (chat, quote(att["file"]))
        r, body = self.request("GET", url, api=False, raw=True, headers={"Range": "bytes=10-19"})
        self.assertEqual((r.status, body), (206, payload[10:20]))
        self.assertEqual(r.getheader("Content-Range"), "bytes 10-19/100")
        r, body = self.request("GET", url, api=False, raw=True, headers={"Range": "bytes=-5"})
        self.assertEqual(body, payload[-5:])
        r, body = self.request("GET", url, api=False, raw=True, headers={"Range": "bytes=200-"})
        self.assertEqual(r.status, 416)
        r, body = self.request("GET", url, api=False, raw=True, headers={"Range": "bytes=0-1,5-6"})
        self.assertEqual((r.status, body), (200, payload))
        etag = r.getheader("ETag")
        r, _ = self.request("GET", url, api=False, raw=True, headers={"If-None-Match": etag})
        self.assertEqual(r.status, 304)
        for name in ("evil.svg", "page.html", "doc.pdf"):
            r, att = self.request("POST", "/api/chats/%s/attachments?name=%s" % (chat, name), b"<svg/>")
            r, _ = self.request("GET", "/files/%s/attachments/%s" % (chat, quote(att["file"])),
                                api=False, raw=True)
            self.assertEqual(r.getheader("Content-Type"), "application/octet-stream", name)
            self.assertTrue(r.getheader("Content-Disposition").startswith("attachment"), name)

    def test_errors(self):
        r, b = self.request("GET", "/api/nope")
        self.assertEqual((r.status, b["error"]["code"]), (404, "not_found"))
        r, b = self.request("PUT", "/api/chats")
        self.assertEqual(r.status, 405)
        r, b = self.request("OPTIONS", "/api/chats")
        self.assertEqual(r.status, 501)
        r, b = self.request("DELETE", "/api/chats/Note%20to%20Self")
        self.assertEqual((r.status, b["error"]["code"]), (403, "default_chat"))
        r, b = self.request("POST", "/api/chats", b"not json")
        self.assertEqual((r.status, b["error"]["code"]), (400, "bad_request"))
        r, b = self.request("POST", "/api/chats/Note%20to%20Self/messages", {"text": "   "})
        self.assertEqual((r.status, b["error"]["code"]), (400, "empty"))
        r, b = self.request("GET", "/api/chats/Note%20to%20Self/messages?before=yesterday")
        self.assertEqual(r.status, 400)
        r, b = self.request("POST", "/api/chats", {"name": "a/b"})
        self.assertEqual((r.status, b["error"]["code"]), (400, "invalid_name"))

    def test_keepalive_after_rejected_body(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/chats", body=b'{"name":"x"}',
                     headers={"Host": "127.0.0.1:%d" % self.port})  # no API header -> 403
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 403)
        self.assertEqual(resp.getheader("Connection"), "close")
        conn.close()


class TestKey(ServerCase):
    """Notes are only served to the browser that opened the launcher's link (other accounts on
    the same computer can reach 127.0.0.1 too)."""

    def test_api_needs_the_key(self):
        r, b = self.request("GET", "/api/chats", key=False)
        self.assertEqual((r.status, b["error"]["code"]), (401, "locked"))
        bad = {"Cookie": "nts-%d=%s" % (self.port, "x" * 43)}
        r, b = self.request("GET", "/api/chats", key=False, headers=bad)
        self.assertEqual(r.status, 401)
        other_port = {"Cookie": "nts-1=%s" % self.store.key}
        r, b = self.request("GET", "/api/chats", key=False, headers=other_port)
        self.assertEqual(r.status, 401)
        r, b = self.request("GET", "/api/chats", key=False, headers={"X-NoteToSelf-Key": self.store.key})
        self.assertEqual(r.status, 200)
        # Other local apps' odd cookies (cookies are shared by every port) don't get in the way.
        messy = {"Cookie": 'w="a;b"; _xsrf=2|ab|; nts-%d=%s; z' % (self.port, self.store.key)}
        r, b = self.request("GET", "/api/chats", key=False, headers=messy)
        self.assertEqual(r.status, 200)

    def test_ping_hides_the_folder_without_the_key(self):
        r, b = self.request("GET", "/api/ping", key=False)
        self.assertEqual((r.status, b["app"]), (200, "note-to-self"))
        self.assertNotIn("root", b)
        r, b = self.request("GET", "/api/ping")
        self.assertEqual(b["root"], self.root)

    def test_files_need_the_key_and_are_not_cached(self):
        chat = quote("Note to Self", safe="")
        r, att = self.request("POST", "/api/chats/%s/attachments?name=k.png" % chat, png_bytes())
        url = "/files/%s/attachments/%s" % (chat, quote(att["file"]))
        r, _ = self.request("GET", url, api=False, key=False, raw=True)
        self.assertEqual(r.status, 401)
        r, _ = self.request("GET", url, api=False, raw=True)
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Cache-Control"), "no-store")

    def test_the_page_itself_is_public(self):
        r, _ = self.request("GET", "/", api=False, key=False, raw=True)
        self.assertEqual(r.status, 200)

    def test_key_link_sets_a_private_cookie(self):
        r, _ = self.request("GET", "/?key=%s&now=2026-10-04" % self.store.key, api=False, key=False,
                            raw=True)
        self.assertEqual(r.status, 303)
        self.assertEqual(r.getheader("Location"), "/?now=2026-10-04")
        cookie = r.getheader("Set-Cookie")
        self.assertTrue(cookie.startswith("nts-%d=%s;" % (self.port, self.store.key)), cookie)
        for attr in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(attr, cookie)
        r, _ = self.request("GET", "/?key=wrong", api=False, key=False, raw=True)
        self.assertEqual((r.status, r.getheader("Location")), (303, "/"))
        self.assertIsNone(r.getheader("Set-Cookie"))


class TestYourData(unittest.TestCase):
    """Drafts, storage stats, Empty trash and Delete all data, against a server of its own
    (Delete all data stops the server)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nts-data-")
        self.root = os.path.join(self.tmp, "notes")
        self.store = Store(self.root, warm=False)
        self.srv = Server(0, self.store)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05},
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        if self.thread.is_alive():
            self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    request = ServerCase.request

    def test_drafts_storage_trash_and_wipe(self):
        chat = quote("Note to Self", safe="")
        r, b = self.request("PUT", "/api/chats/%s/draft" % chat, {"text": "half a thought"})
        self.assertEqual((r.status, b["draft"]["text"]), (200, "half a thought"))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "Note to Self", ".draft.md")))
        r, b = self.request("GET", "/api/chats")
        self.assertEqual(b["chats"][0]["draft"]["text"], "half a thought")
        r, b = self.request("PUT", "/api/chats/%s/draft" % chat, {"text": "  "})
        self.assertEqual(b, {"draft": None})
        self.assertFalse(os.path.exists(os.path.join(self.root, "Note to Self", ".draft.md")))

        r, sent = self.request("POST", "/api/chats/%s/messages" % chat, {"text": "keep me"})
        r, sent2 = self.request("POST", "/api/chats/%s/messages" % chat, {"text": "bin me"})
        m = sent2["message"]
        self.request("POST", "/api/chats/%s/messages/delete" % chat, {"items": [{"id": m["id"], "hash": m["hash"]}]})
        r, st = self.request("GET", "/api/storage")
        self.assertEqual((r.status, st["notes"], st["chats"]), (200, 1, 1))
        self.assertGreater(st["trash"]["bytes"], 0)
        self.assertEqual(st["root"], self.root)

        r, st = self.request("POST", "/api/trash/empty", {})
        self.assertEqual((r.status, st["trash"]["bytes"], st["notes"]), (200, 0, 1))
        self.assertFalse(os.path.exists(os.path.join(self.root, ".trash")))

        r, b = self.request("POST", "/api/wipe", {})
        self.assertEqual((r.status, b["error"]["code"]), (400, "bad_request"))
        self.assertTrue(os.path.isdir(self.root))
        r, b = self.request("POST", "/api/wipe", {"confirm": "delete all data"})
        self.assertEqual((r.status, b["ok"]), (200, True))
        self.assertFalse(os.path.exists(self.root))
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive(), "the server stops after deleting everything")
        self.assertTrue(self.srv.wiped)

    def test_browser_setting(self):
        r, b = self.request("GET", "/api/settings")
        self.assertEqual((r.status, b["browser"]), (200, "auto"))
        self.assertIsInstance(b["browsers"], list)
        r, b = self.request("PUT", "/api/settings", {"browser": "Firefox"})
        self.assertEqual((r.status, b["browser"]), (200, "firefox"))
        self.assertEqual(self.store.settings(), {"browser": "firefox"})
        r, b = self.request("PUT", "/api/settings", {"browser": "netscape"})
        self.assertEqual(r.status, 400)
        r, b = self.request("PUT", "/api/settings", {"browser": "auto"})
        self.assertEqual(self.store.settings(), {})
        r, b = self.request("GET", "/api/settings", key=False)
        self.assertEqual(r.status, 401)

    def test_open_another_window_in_the_chosen_browser(self):
        self.store.set_settings({"browser": "firefox"})
        with mock.patch.object(browsers, "open_window", return_value=True) as ow, \
                mock.patch.dict(os.environ, {"NOTETOSELF_BROWSER": ""}):
            r, b = self.request("POST", "/api/open", {})
        self.assertEqual((r.status, b), (200, {"ok": True}))
        url, choice = ow.call_args[0][:2]
        self.assertEqual(url, "http://127.0.0.1:%d/?key=%s" % (self.port, self.store.key))
        self.assertEqual(choice, "firefox")

    def test_quit_needs_the_key_and_stops_the_server(self):
        r, b = self.request("POST", "/api/quit", {}, key=False)
        self.assertEqual(r.status, 401)
        self.assertTrue(self.thread.is_alive())
        r, b = self.request("POST", "/api/quit", {})
        self.assertEqual((r.status, b), (200, {"ok": True}))
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.assertTrue(self.srv.quitting)
        self.assertTrue(os.path.isdir(self.root))  # quitting keeps the notes, of course


if __name__ == "__main__":
    unittest.main()
