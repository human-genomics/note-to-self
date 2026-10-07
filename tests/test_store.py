"""Store: chats, sending, paging, edit/delete with trash, attachments, search, durability."""

from __future__ import annotations

import datetime as dt
import io
import os
import shutil
import struct
import tempfile
import threading
import unittest
import zlib
from unittest import mock

from nts import grammar
from nts import store as store_mod
from nts.store import ApiError, Store, sanitize_filename, snippet, validate_chat_name

D = dt.datetime


def png_bytes(w=3, h=2):
    def chunk(t, data):
        return struct.pack(">I", len(data)) + t + data + struct.pack(">I", zlib.crc32(t + data))
    raw = b"".join(b"\x00" + b"\x80\x80\x80" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def jpeg_bytes(w, h, orientation=1):
    exif_tiff = (b"MM\x00\x2a\x00\x00\x00\x08" + struct.pack(">H", 1)
                 + struct.pack(">HHIHH", 0x0112, 3, 1, orientation, 0) + b"\x00\x00\x00\x00")
    app1 = b"Exif\x00\x00" + exif_tiff
    sof = b"\x08" + struct.pack(">HH", h, w) + b"\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    return (b"\xff\xd8" + b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1
            + b"\xff\xc0" + struct.pack(">H", len(sof) + 2) + sof + b"\xff\xd9")


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nts-test-")
        self.root = os.path.join(self.tmp, "notes")
        self.warnings = []
        self.s = Store(self.root, warn=self.warnings.append, warm=False)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def day(self, chat="Note to Self", date="2026-10-04"):
        with open(os.path.join(self.root, chat, date + ".md"), "rb") as f:
            return f.read().decode("utf-8")

    def write_day(self, raw, chat="Note to Self", date="2026-10-04"):
        with open(os.path.join(self.root, chat, date + ".md"), "wb") as f:
            f.write(raw.encode("utf-8"))

    def read(self, *parts):
        with open(os.path.join(self.root, *parts), encoding="utf-8") as f:
            return f.read()

    def send(self, text, at, chat="Note to Self", atts=None):
        return self.s.send(chat, text, atts, now=at)["message"]


class TestSend(StoreCase):
    def test_send_writes_markdown_and_journal(self):
        m = self.send("Buy oat milk", D(2026, 10, 4, 9, 14, 2))
        self.assertEqual(m["id"], "2026-10-04T091402-0")
        self.assertEqual(self.day(), "## 09:14:02\n\nBuy oat milk\n")
        self.send("second\nline", D(2026, 10, 4, 9, 15, 0))
        self.assertEqual(self.day(), "## 09:14:02\n\nBuy oat milk\n\n## 09:15:00\n\nsecond\nline\n")
        journal = self.read(".trash", "sent", "2026-10.md")
        self.assertIn("Buy oat milk", journal)
        self.assertIn("second\nline", journal)

    def test_response_and_rev(self):
        before = self.s.chat("Note to Self")["rev"]
        r = self.s.send("Note to Self", "hi", None, now=D(2026, 10, 4, 9, 0, 0))
        self.assertEqual(r["rev"]["before"], before)
        self.assertNotEqual(r["rev"]["after"], before)
        self.assertEqual(r["rev"]["after"], self.s.chat("Note to Self")["rev"])
        m = r["message"]
        self.assertEqual(set(m), {"id", "hash", "date", "time", "ts", "edited", "text", "attachments"})
        self.assertEqual(m["ts"], int(D(2026, 10, 4, 9, 0, 0).timestamp() * 1000))

    def test_append_to_unterminated_and_crlf_files(self):
        self.write_day("## 08:00:00\r\n\r\nfrom windows")
        self.send("next", D(2026, 10, 4, 9, 0, 0))
        page = self.s.page("Note to Self")
        texts = [m["text"] for d in page["days"] for m in d["messages"]]
        self.assertEqual(texts, ["from windows", "next"])
        self.assertTrue(self.day().startswith("## 08:00:00\r\n\r\nfrom windows\n\n## 09:00:00"))

    def test_same_second(self):
        a = self.send("a", D(2026, 10, 4, 9, 0, 0))
        b = self.send("b", D(2026, 10, 4, 9, 0, 0))
        self.assertEqual((a["id"], b["id"]), ("2026-10-04T090000-0", "2026-10-04T090000-1"))

    def test_validation(self):
        with self.assertRaises(ApiError) as c:
            self.s.send("Note to Self", "  \n\t ", None)
        self.assertEqual(c.exception.code, "empty")
        with self.assertRaises(ApiError) as c:
            self.s.send("Note to Self", "a" * 65537, None)
        self.assertEqual((c.exception.status, c.exception.code), (413, "too_long"))
        with self.assertRaises(ApiError) as c:
            self.s.send("Nope", "x", None)
        self.assertEqual(c.exception.status, 404)
        with self.assertRaises(ApiError) as c:
            self.s.send("Note to Self", "x", [{"file": "missing.png", "name": "m"}])
        self.assertEqual(c.exception.status, 400)
        with self.assertRaises(ApiError):
            self.s.send("Note to Self", "x", [{"file": "../x", "name": "m"}])
        # Lone surrogates from JSON are replaced, not crashed on.
        m = self.s.send("Note to Self", "a\ud800b", None, now=D(2026, 10, 4, 9, 0, 0))["message"]
        self.assertEqual(m["text"], "a?b")

    def test_concurrent_sends(self):
        errors = []

        def worker(i):
            try:
                self.s.send("Note to Self", "note %d" % i, None, now=D(2026, 10, 4, 10, 0, i % 3))
            except Exception as e:  # pragma: no cover
                errors.append(e)
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        texts = sorted(m["text"] for d in self.s.page("Note to Self")["days"] for m in d["messages"])
        self.assertEqual(texts, sorted("note %d" % i for i in range(20)))

    def test_file_order_is_kept_when_clock_goes_backwards(self):
        self.send("first", D(2026, 10, 4, 1, 59, 0))
        self.send("second (after DST fall-back)", D(2026, 10, 4, 1, 5, 0))
        msgs = self.s.page("Note to Self")["days"][0]["messages"]
        self.assertEqual([m["text"] for m in msgs], ["first", "second (after DST fall-back)"])


class TestEditDelete(StoreCase):
    def setUp(self):
        super().setUp()
        self.write_day("## 09:00:00\n\nalpha\n\n## 09:01:00\n\nbeta\n\n## 09:02:00\n\ngamma\n")

    def msgs(self):
        return self.s.page("Note to Self")["days"][0]["messages"]

    def test_edit_locality_and_trash(self):
        b = self.msgs()[1]
        r = self.s.edit("Note to Self", b["id"], b["hash"], "BETA two")
        self.assertTrue(r["message"]["edited"])
        self.assertEqual(r["message"]["text"], "BETA two")
        self.assertEqual(self.day(), "## 09:00:00\n\nalpha\n\n## 09:01:00 (edited)\n\nBETA two\n\n"
                                     "## 09:02:00\n\ngamma\n")
        trash = self.read(".trash", "Note to Self", "2026-10-04.md")
        self.assertIn("<!-- replaced ", trash)
        self.assertIn("## 09:01:00\n\nbeta", trash)
        self.assertEqual(r["days"][0]["date"], "2026-10-04")
        self.assertNotEqual(r["rev"]["before"], r["rev"]["after"])

    def test_edit_noop(self):
        b = self.msgs()[0]
        before = self.day()
        r = self.s.edit("Note to Self", b["id"], b["hash"], "alpha\n")
        self.assertEqual(self.day(), before)
        self.assertEqual(r["rev"]["before"], r["rev"]["after"])
        self.assertFalse(os.path.exists(os.path.join(self.root, ".trash", "Note to Self")))

    def test_edit_conflict_after_external_change(self):
        b = self.msgs()[1]
        self.write_day(self.day().replace("beta", "beta changed in VS Code"))
        with self.assertRaises(ApiError) as c:
            self.s.edit("Note to Self", b["id"], b["hash"], "mine")
        self.assertEqual((c.exception.status, c.exception.code), (409, "conflict"))
        self.assertEqual(c.exception.extra["days"][0]["messages"][1]["text"], "beta changed in VS Code")

    def test_edit_empty_rejected(self):
        b = self.msgs()[0]
        with self.assertRaises(ApiError) as c:
            self.s.edit("Note to Self", b["id"], b["hash"], "   ")
        self.assertEqual(c.exception.code, "empty")

    def test_delete_single_and_multi(self):
        m = self.msgs()
        r = self.s.delete("Note to Self", [{"id": m[1]["id"], "hash": m[1]["hash"]}])
        self.assertEqual(r["deleted"], 1)
        self.assertEqual(self.day(), "## 09:00:00\n\nalpha\n\n## 09:02:00\n\ngamma\n")
        m = self.msgs()
        r = self.s.delete("Note to Self", [{"id": x["id"], "hash": x["hash"]} for x in m])
        self.assertEqual(r["deleted"], 2)
        self.assertEqual(r["days"], [{"date": "2026-10-04", "messages": []}])
        self.assertFalse(os.path.exists(os.path.join(self.root, "Note to Self", "2026-10-04.md")))
        trash = self.read(".trash", "Note to Self", "2026-10-04.md")
        self.assertEqual(trash.count("<!-- deleted "), 3)

    def test_delete_all_or_nothing(self):
        m = self.msgs()
        before = self.day()
        with self.assertRaises(ApiError) as c:
            self.s.delete("Note to Self", [{"id": m[0]["id"], "hash": m[0]["hash"]},
                                           {"id": m[1]["id"], "hash": "000000000000"}])
        self.assertEqual(c.exception.status, 409)
        self.assertEqual(self.day(), before)

    def test_delete_moves_attachments_to_trash(self):
        att = self.s.upload("Note to Self", "pic.png", "image/png", io.BytesIO(png_bytes()),
                            len(png_bytes()), now=D(2026, 10, 4, 9, 5, 0))
        m = self.send("with pic", D(2026, 10, 4, 9, 5, 0), atts=[{"file": att["file"], "name": "pic.png"}])
        self.s.delete("Note to Self", [{"id": m["id"], "hash": m["hash"]}])
        self.assertFalse(os.path.exists(os.path.join(self.root, "Note to Self", "attachments", att["file"])))
        self.assertTrue(os.path.exists(os.path.join(self.root, ".trash", "Note to Self", "attachments",
                                                    att["file"])))

    def test_failed_replace_leaves_original(self):
        b = self.msgs()[0]
        before = self.day()
        with mock.patch.object(store_mod.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.s.edit("Note to Self", b["id"], b["hash"], "new")
        self.assertEqual(self.day(), before)
        leftovers = [n for n in os.listdir(os.path.join(self.root, "Note to Self")) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_windows_lock_retry(self):
        calls = {"n": 0}
        real = os.replace

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError("locked")
            return real(src, dst)
        b = self.msgs()[0]
        with mock.patch.object(store_mod, "IS_WINDOWS", True), \
                mock.patch.object(store_mod.os, "replace", side_effect=flaky), \
                mock.patch.object(store_mod.time, "sleep"):
            self.s.edit("Note to Self", b["id"], b["hash"], "retried")
        self.assertIn("retried", self.day())
        self.assertEqual(calls["n"], 3)

    def test_rev_changes_on_write_not_read(self):
        r1 = self.s.chat("Note to Self")["rev"]
        self.s.page("Note to Self")
        self.s.search("alpha")
        self.assertEqual(self.s.chat("Note to Self")["rev"], r1)
        self.send("new", D(2026, 10, 4, 9, 3, 0))
        self.assertNotEqual(self.s.chat("Note to Self")["rev"], r1)


class TestPaging(StoreCase):
    def setUp(self):
        super().setUp()
        # 10 days, 30 messages each, plus empty day files that must be skipped.
        for day in range(1, 11):
            raw = "\n".join(grammar.format_block("10:%02d:00" % i, "d%d m%d" % (day, i), [])
                            for i in range(30))
            self.write_day(raw, date="2026-09-%02d" % day)
        self.write_day("", date="2026-09-11")
        self.write_day("## 10:00:00\n\n\n", date="2026-09-12")

    def dates(self, r):
        return [d["date"] for d in r["days"]]

    def test_default_newest(self):
        r = self.s.page("Note to Self", limit=100)
        self.assertEqual(self.dates(r), ["2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10"])
        self.assertTrue(r["hasBefore"])
        self.assertFalse(r["hasAfter"])

    def test_before_after(self):
        r = self.s.page("Note to Self", before="2026-09-04", limit=50)
        self.assertEqual(self.dates(r), ["2026-09-02", "2026-09-03"])
        self.assertTrue(r["hasBefore"] and r["hasAfter"])
        r = self.s.page("Note to Self", before="2026-09-02", limit=1000)
        self.assertEqual(self.dates(r), ["2026-09-01"])
        self.assertFalse(r["hasBefore"])
        r = self.s.page("Note to Self", after="2026-09-08", limit=10)
        self.assertEqual(self.dates(r), ["2026-09-09"])
        self.assertTrue(r["hasAfter"] and r["hasBefore"])

    def test_around(self):
        r = self.s.page("Note to Self", around="2026-09-05", limit=60)
        self.assertEqual(self.dates(r), ["2026-09-04", "2026-09-05", "2026-09-06"])
        r = self.s.page("Note to Self", around="2026-12-25", limit=10)
        self.assertEqual(self.dates(r)[-1], "2026-09-10")
        self.assertFalse(r["hasAfter"])

    def test_from_to_and_limits(self):
        r = self.s.page("Note to Self", frm="2026-09-03", to="2026-09-05")
        self.assertEqual(self.dates(r), ["2026-09-03", "2026-09-04", "2026-09-05"])
        r = self.s.page("Note to Self", frm="2026-09-09")
        self.assertEqual(self.dates(r), ["2026-09-09", "2026-09-10"])
        self.assertFalse(r["hasAfter"])
        r = self.s.page("Note to Self", limit=0)
        self.assertEqual(len(self.dates(r)), 1)
        for bad in ("2026-9-1", "x", "2026-02-30"):
            with self.assertRaises(ApiError):
                self.s.page("Note to Self", before=bad)


class TestChats(StoreCase):
    def test_validation_table(self):
        bad = ["", "   ", "a/b", "a\\b", "a:b", 'a"b', "a?b", "a*b", "a|b", "a<b", ".hidden",
               "trailing.", "CON", "con.txt", "LPT9", "nul", "x" * 65, "tab\there"]
        for name in bad:
            with self.assertRaises(ApiError, msg=name):
                validate_chat_name(name, [])
        self.assertEqual(validate_chat_name("  Ideas  ", []), "Ideas")
        with self.assertRaises(ApiError) as c:
            validate_chat_name("ideas", ["Ideas"])
        self.assertEqual(c.exception.code, "name_taken")
        with self.assertRaises(ApiError):
            validate_chat_name("note to self", ["Note to Self"])
        # NFD duplicate of an NFC name
        with self.assertRaises(ApiError):
            validate_chat_name("Cafe\u0301", ["Caf\u00e9"])
        self.assertEqual(validate_chat_name("IDEAS", ["Ideas"], current="Ideas"), "IDEAS")

    def test_create_rename_delete(self):
        c = self.s.create_chat("Ideas")
        self.assertEqual(c["name"], "Ideas")
        self.assertFalse(c["isDefault"])
        self.send("idea one", D(2026, 10, 4, 9, 0, 0), chat="Ideas")
        c = self.s.rename_chat("Ideas", "Big ideas")
        self.assertEqual(c["name"], "Big ideas")
        self.assertEqual(c["last"]["text"], "idea one")
        self.s.delete_chat("Big ideas")
        self.assertNotIn("Big ideas", [x["name"] for x in self.s.list_chats()])
        trashed = os.listdir(os.path.join(self.root, ".trash", "chats"))
        self.assertTrue(trashed[0].startswith("Big ideas "))

    def test_default_chat_protected(self):
        for fn in (lambda: self.s.rename_chat("Note to Self", "X"),
                   lambda: self.s.delete_chat("Note to Self")):
            with self.assertRaises(ApiError) as c:
                fn()
            self.assertEqual((c.exception.status, c.exception.code), (403, "default_chat"))

    def test_ordering_and_last(self):
        self.s.create_chat("Old")
        self.s.create_chat("Newer")
        self.send("old one", D(2026, 10, 1, 9, 0, 0), chat="Old")
        self.send("newest", D(2026, 10, 3, 9, 0, 0), chat="Newer")
        self.send("middle", D(2026, 10, 2, 9, 0, 0))
        names = [c["name"] for c in self.s.list_chats()]
        self.assertEqual(names, ["Newer", "Note to Self", "Old"])
        newer = self.s.list_chats()[0]
        self.assertEqual(newer["last"], {"text": "newest", "attachments": []})
        self.assertEqual(newer["lastTs"], int(D(2026, 10, 3, 9, 0, 0).timestamp() * 1000))

    def test_conflict_copies_warned_and_ignored(self):
        self.write_day("## 10:00:00\n\nreal\n")
        with open(os.path.join(self.root, "Note to Self", "2026-10-04 2.md"), "w") as f:
            f.write("## 11:00:00\n\ncopy\n")
        texts = [m["text"] for d in self.s.page("Note to Self")["days"] for m in d["messages"]]
        self.assertEqual(texts, ["real"])
        self.assertTrue(any("sync-conflict" in w for w in self.warnings))

    def test_stale_temp_files_purged(self):
        p = os.path.join(self.root, "Note to Self", ".2026-10-04.md.abc.tmp")
        open(p, "w").close()
        old = dt.datetime.now().timestamp() - 3600
        os.utime(p, (old, old))
        fresh = os.path.join(self.root, "Note to Self", ".x.part")
        open(fresh, "w").close()
        Store(self.root, warm=False)
        self.assertFalse(os.path.exists(p))
        self.assertTrue(os.path.exists(fresh))


class TestAttachments(StoreCase):
    def test_sanitize_table(self):
        cases = {
            "IMG 2034.PNG": "IMG-2034.png",
            "../../etc/passwd": "passwd",
            "C:\\Users\\me\\report final.pdf": "report-final.pdf",
            ".bashrc": "file.bashrc",
            "weird*&^%name!!.txt": "weirdname.txt",
            "a" * 200 + ".jpg": "a" * 80 + ".jpg",
            "": "file",
            "café ünïcode.md": "café-ünïcode.md",
            "noext": "noext",
        }
        for src, want in cases.items():
            self.assertEqual(sanitize_filename(src), want, src)
        self.assertEqual(sanitize_filename("blob", "image/png"), "blob.png")

    def test_upload_and_collisions(self):
        data = png_bytes(5, 4)
        now = D(2026, 10, 4, 14, 23, 5)
        a = self.s.upload("Note to Self", "IMG 2034.png", "image/png", io.BytesIO(data), len(data), now=now)
        b = self.s.upload("Note to Self", "IMG 2034.png", "image/png", io.BytesIO(data), len(data), now=now)
        self.assertEqual(a["file"], "2026-10-04_142305_IMG-2034.png")
        self.assertEqual(b["file"], "2026-10-04_142305_IMG-2034-1.png")
        self.assertEqual((a["kind"], a["size"], a["w"], a["h"], a["name"]), ("image", len(data), 5, 4, "IMG 2034.png"))
        m = self.send("Photos", now, atts=[{"file": a["file"], "name": a["name"]}])
        self.assertEqual(m["attachments"][0]["url"],
                         "/files/Note%20to%20Self/attachments/2026-10-04_142305_IMG-2034.png")
        self.assertIn("![IMG 2034.png](attachments/2026-10-04_142305_IMG-2034.png)\n\nPhotos", self.day())

    def test_upload_limits_and_abort(self):
        with self.assertRaises(ApiError) as c:
            self.s.upload("Note to Self", "x", None, io.BytesIO(b""), None)
        self.assertEqual(c.exception.status, 411)
        with self.assertRaises(ApiError) as c:
            self.s.upload("Note to Self", "x", None, io.BytesIO(b""), 100 * 1024 * 1024 + 1)
        self.assertEqual(c.exception.status, 413)
        with self.assertRaises(ApiError):
            self.s.upload("Note to Self", "x.bin", None, io.BytesIO(b"short"), 100)
        adir = os.path.join(self.root, "Note to Self", "attachments")
        self.assertEqual(os.listdir(adir), [])

    def test_image_sizes(self):
        cases = [
            (png_bytes(7, 3), (7, 3)),
            (b"GIF89a" + struct.pack("<HH", 9, 4) + b"\x00" * 10, (9, 4)),
            (jpeg_bytes(640, 480), (640, 480)),
            (jpeg_bytes(640, 480, orientation=6), (480, 640)),
            (b"RIFF\x00\x00\x00\x00WEBPVP8X" + b"\x00" * 8 + (99).to_bytes(3, "little")
             + (49).to_bytes(3, "little") + b"\x00" * 10, (100, 50)),
            (b"not an image", None),
        ]
        for i, (data, want) in enumerate(cases):
            p = os.path.join(self.tmp, "img%d" % i)
            with open(p, "wb") as f:
                f.write(data)
            self.assertEqual(store_mod.image_size(p), want, i)

    def test_attachment_path_safety(self):
        a = self.s.upload("Note to Self", "a.txt", None, io.BytesIO(b"hi"), 2)
        self.assertTrue(self.s.attachment_path("Note to Self", a["file"]).endswith(a["file"]))
        outside = os.path.join(self.tmp, "secret.txt")
        open(outside, "w").close()
        link = os.path.join(self.root, "Note to Self", "attachments", "link.txt")
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):  # pragma: no cover - Windows without privilege
            link = None
        for bad in ("../2026-10-04.md", ".hidden", "missing.txt") + (("link.txt",) if link else ()):
            with self.assertRaises(ApiError, msg=bad):
                self.s.attachment_path("Note to Self", bad)


class TestSearch(StoreCase):
    def setUp(self):
        super().setUp()
        self.s.create_chat("Ideas")
        self.send("Gardening plan with seeds that sprout", D(2026, 10, 2, 9, 0, 0))
        self.send("café résumé notes", D(2026, 10, 3, 9, 0, 0))
        self.send("gardening ideas for spring", D(2026, 10, 4, 9, 0, 0), chat="Ideas")
        self.send("unrelated", D(2026, 10, 4, 10, 0, 0))

    def test_prefix_and_newest_first(self):
        r = self.s.search("gar")
        self.assertEqual([m["chat"] for m in r["messages"]], ["Ideas", "Note to Self"])
        self.assertEqual(self.s.search("gardening seeds")["messages"][0]["date"], "2026-10-02")
        self.assertEqual(self.s.search("dening")["messages"], [])  # prefix only
        self.assertEqual(self.s.search("gardening zebra")["messages"], [])  # AND

    def test_accents_chat_filter_and_names(self):
        self.assertEqual(len(self.s.search("cafe resume")["messages"]), 1)
        self.assertEqual(len(self.s.search("gardening", chat="Ideas")["messages"]), 1)
        self.assertEqual(self.s.search("ide")["chats"], [{"name": "Ideas", "isDefault": False}])
        self.assertEqual(self.s.search("gardening", chat="Ideas")["chats"], [])
        self.assertEqual(self.s.search("!!! ...")["messages"], [])

    def test_caps(self):
        for i in range(105):
            self.send("bulk item %d" % i, D(2026, 10, 5, 9, i // 60, i % 60), chat="Ideas")
        r = self.s.search("bulk", chat="Ideas")
        self.assertEqual(len(r["messages"]), 100)
        self.assertTrue(r["truncated"])
        self.assertEqual(r["messages"][0]["snippet"], [["bulk", True], [" item 104", False]])

    def test_snippet(self):
        text = "one two three four five six seven eight nine ten eleven twelve thirteen"
        segs = snippet(text, ["six"])
        self.assertEqual(segs, [["...four five ", False], ["six", True],
                                [" seven eight nine ten eleven twelve thirteen", False]])
        segs = snippet("alpha beta", ["beta"])
        self.assertEqual(segs, [["alpha ", False], ["beta", True]])
        long = " ".join("w%d" % i for i in range(30))
        segs = snippet(long, ["w0"])
        self.assertEqual(segs[-1][0][-3:], "...")


class TestDurabilityHelpers(unittest.TestCase):
    def test_fullfsync_used_on_mac(self):
        with tempfile.TemporaryFile() as f, mock.patch.object(store_mod, "IS_MAC", True):
            fake = mock.MagicMock()
            fake.F_FULLFSYNC = 51
            with mock.patch.dict("sys.modules", {"fcntl": fake}):
                store_mod.fsync_fd(f.fileno())
            fake.fcntl.assert_called_once_with(f.fileno(), 51)


class TestYourData(StoreCase):
    """Where everything lives, drafts, storage stats, Empty trash and Delete all data."""

    def test_marker_and_key(self):
        marker = os.path.join(self.root, store_mod.MARKER)
        key = store_mod.folder_key(self.root)
        self.assertEqual(key, self.s.key)
        self.assertRegex(key, r"^[A-Za-z0-9_-]{43}$")
        self.assertEqual(Store(self.root, warm=False).key, key)  # stable, so the browser stays signed in
        if os.name != "nt":
            self.assertEqual(os.stat(marker).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(self.root).st_mode & 0o077, 0)  # other accounts can't look in
        with open(marker, "w") as f:
            f.write("{damaged")
        fresh = Store(self.root, warm=False)
        self.assertNotEqual(fresh.key, key)
        self.assertEqual(store_mod.folder_key(self.root), fresh.key)

    def test_settings_are_kept_with_the_key(self):
        key = self.s.key
        self.assertEqual(self.s.settings(), {})
        self.assertEqual(self.s.set_settings({"browser": "firefox"}), {"browser": "firefox"})
        again = Store(self.root, warm=False)
        self.assertEqual((again.key, again.settings()), (key, {"browser": "firefox"}))
        self.assertEqual(store_mod.folder_settings(self.root), {"browser": "firefox"})
        self.assertEqual(self.s.set_settings({"browser": None}), {})
        if os.name != "nt":
            self.assertEqual(os.stat(os.path.join(self.root, store_mod.MARKER)).st_mode & 0o777, 0o600)

    def test_refuses_a_folder_with_other_files(self):
        other = os.path.join(self.tmp, "Documents")
        os.makedirs(other)
        with open(os.path.join(other, "report.docx"), "w") as f:
            f.write("not ours")
        with self.assertRaises(store_mod.FolderError):
            Store(other, warm=False)
        self.assertEqual(os.listdir(other), ["report.docx"])  # untouched
        empty_ish = os.path.join(self.tmp, "fresh")
        os.makedirs(empty_ish)
        open(os.path.join(empty_ish, ".DS_Store"), "w").close()
        Store(empty_ish, warm=False)  # OS litter doesn't count
        older = os.path.join(self.tmp, "older")  # made by a version without the marker file
        os.makedirs(os.path.join(older, "Note to Self"))
        Store(older, warm=False)
        self.assertIsNotNone(store_mod.folder_key(older))

    def test_drafts(self):
        self.assertIsNone(self.s.list_chats()[0]["draft"])
        d = self.s.set_draft("Note to Self", "line one\nline two")["draft"]
        self.assertEqual(d["text"], "line one\nline two")
        self.assertEqual(self.read("Note to Self", ".draft.md"), "line one\nline two")
        self.assertEqual(self.s.list_chats()[0]["draft"]["text"], "line one\nline two")
        self.s.create_chat("Ideas")
        self.s.set_draft("Ideas", "idea")
        self.s.rename_chat("Ideas", "Plans")  # the draft moves with the folder
        self.assertEqual(self.s.chat("Plans")["draft"]["text"], "idea")
        self.assertEqual(self.s.set_draft("Note to Self", " \n "), {"draft": None})
        self.assertFalse(os.path.exists(os.path.join(self.root, "Note to Self", ".draft.md")))
        self.assertEqual(self.s.page("Note to Self")["days"], [])  # never shown as a note
        with self.assertRaises(ApiError):
            self.s.set_draft("Note to Self", 42)

    def test_storage_and_empty_trash(self):
        self.s.create_chat("Ideas")
        self.send("one", D(2026, 10, 3, 9, 0, 0))
        self.send("two", D(2026, 10, 4, 9, 0, 0), chat="Ideas")
        gone = self.send("three", D(2026, 10, 4, 9, 1, 0))
        att = self.s.upload("Note to Self", "pic.png", "image/png", io.BytesIO(png_bytes()), len(png_bytes()))
        self.s.send("Note to Self", "", [att], now=D(2026, 10, 4, 9, 2, 0))
        self.s.set_draft("Ideas", "later")
        self.s.delete("Note to Self", [{"id": gone["id"], "hash": gone["hash"]}])
        st = self.s.storage()
        self.assertEqual((st["chats"], st["notes"], st["drafts"]), (2, 3, 1))
        self.assertEqual(st["attachments"], {"count": 1, "bytes": len(png_bytes())})
        self.assertGreater(st["trash"]["bytes"], 0)  # the deleted note and the send log
        self.assertGreater(st["totalBytes"], st["trash"]["bytes"])
        st = self.s.empty_trash()
        self.assertEqual(st["trash"], {"files": 0, "bytes": 0})
        self.assertFalse(os.path.exists(os.path.join(self.root, ".trash")))
        self.assertEqual(st["notes"], 3)
        self.s.empty_trash()  # emptying an empty trash is fine

    def test_wipe_removes_everything_and_reads_dont_recreate_it(self):
        key = self.s.key
        self.send("secret", D(2026, 10, 4, 9, 0, 0))
        self.s.set_draft("Note to Self", "draft")
        self.s.wipe()
        self.assertFalse(os.path.exists(self.root))
        # Reads (polling, the chat list, Settings) must not bring the folder back.
        chats = self.s.list_chats()
        self.assertEqual([(c["name"], c["last"], c["draft"]) for c in chats], [("Note to Self", None, None)])
        self.assertEqual(self.s.page("Note to Self")["days"], [])
        self.assertEqual(self.s.search("secret")["messages"], [])
        self.assertFalse(self.s.storage()["exists"])
        self.assertFalse(os.path.exists(self.root))
        # A new note starts a fresh folder, with the same key so the open window keeps working.
        self.send("again", D(2026, 10, 4, 10, 0, 0))
        self.assertEqual(store_mod.folder_key(self.root), key)

    def test_wipe_refuses_unsafe_folders(self):
        with mock.patch.object(store_mod.os.path, "expanduser", return_value=self.root):
            with self.assertRaises(ApiError) as cm:
                self.s.wipe()  # the notes folder is the home folder
        self.assertEqual(cm.exception.code, "unsafe")
        self.assertTrue(os.path.isdir(self.root))
        with open(os.path.join(self.root, "Note to Self", "x.md"), "w") as f:
            f.write("x")
        os.remove(os.path.join(self.root, store_mod.MARKER))
        os.rename(os.path.join(self.root, "Note to Self"), os.path.join(self.root, "Elsewhere"))
        with self.assertRaises(ApiError):
            self.s.wipe()  # no longer looks like a Note to Self folder
        self.assertTrue(os.path.isdir(self.root))

    @unittest.skipIf(os.name == "nt", "symlinks")
    def test_wipe_through_a_symlink_never_touches_outside_files(self):
        outside = os.path.join(self.tmp, "outside.txt")
        with open(outside, "w") as f:
            f.write("keep")
        os.symlink(outside, os.path.join(self.root, "Note to Self", "link.txt"))
        real = os.path.join(self.tmp, "real-notes")
        os.rename(self.root, real)
        os.symlink(real, self.root)  # e.g. ~/NoteToSelf -> a synced folder
        self.s.wipe()
        self.assertFalse(os.path.lexists(self.root))
        self.assertFalse(os.path.exists(real))
        with open(outside) as f:
            self.assertEqual(f.read(), "keep")


@unittest.skipIf(os.name == "nt", "POSIX paths")
class TestRevealLinux(unittest.TestCase):
    """'Show in folder' on Linux: ask the file manager over D-Bus, else fall back to xdg-open."""

    def reveal(self, path, select, outcomes):
        calls = []

        def popen(cmd, **kw):
            calls.append(cmd)
            result = outcomes.get(cmd[0], 0)
            if result == "missing":
                raise FileNotFoundError(cmd[0])
            proc = mock.Mock()
            if result == "slow":
                proc.wait.side_effect = store_mod.subprocess.TimeoutExpired(cmd, 3)
            else:
                proc.wait.return_value = result
            return proc

        with mock.patch.object(store_mod, "IS_MAC", False), mock.patch.object(store_mod, "IS_WINDOWS", False), \
                mock.patch.object(store_mod.subprocess, "Popen", side_effect=popen):
            store_mod._open_in_file_manager(path, select)
        return calls

    def test_selects_the_file_over_dbus(self):
        calls = self.reveal("/home/u/NoteToSelf/Note to Self/2026-10-06.md", True, {})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ["gdbus", "call"])
        self.assertIn("org.freedesktop.FileManager1.ShowItems", calls[0])
        self.assertIn("['file:///home/u/NoteToSelf/Note%20to%20Self/2026-10-06.md']", calls[0])

    def test_folder_uses_show_folders_and_escapes_quotes(self):
        calls = self.reveal("/home/u/Sam's, notes", False, {})
        self.assertIn("org.freedesktop.FileManager1.ShowFolders", calls[0])
        self.assertIn("['file:///home/u/Sam%27s%2C%20notes']", calls[0])

    def test_non_utf8_folder_name(self):
        path = os.fsdecode(b"/home/u/caf\xe9")  # possible on Linux filesystems
        calls = self.reveal(path, False, {})
        self.assertIn("['file:///home/u/caf%E9']", calls[0])

    def test_fallbacks(self):
        # No gdbus: use dbus-send.
        calls = self.reveal("/n/a.md", True, {"gdbus": "missing"})
        self.assertEqual([c[0] for c in calls], ["gdbus", "dbus-send"])
        # No FileManager1 service (the call fails): open the folder with xdg-open.
        calls = self.reveal("/n/a.md", True, {"gdbus": 1})
        self.assertEqual(calls[-1], ["xdg-open", "/n"])
        self.assertEqual(len(calls), 2)
        # File manager still starting after 3 s: trust it, don't open a second window.
        calls = self.reveal("/n/a.md", True, {"gdbus": "slow"})
        self.assertEqual(len(calls), 1)
        # Nothing available at all.
        with self.assertRaises(ApiError):
            self.reveal("/n", False, {"gdbus": "missing", "dbus-send": "missing", "xdg-open": "missing"})


if __name__ == "__main__":
    unittest.main()
