"""Day-file grammar: lossless parsing (G1), edit locality (G2), round-trip fidelity (G3)."""

from __future__ import annotations

import os
import random
import unittest

from nts import grammar
from nts.grammar import Att

FUZZ_CASES = int(os.environ.get("NTS_FUZZ", "100000"))


def join_blocks(parts):
    """Build a file the way the store does: append each block with a separator."""
    raw = ""
    for p in parts:
        raw += grammar.separator(raw[-4:].encode("utf-8", "surrogateescape")) + p
    return raw


class TestParse(unittest.TestCase):
    def test_basic(self):
        raw = "## 09:14:02\n\nBuy oat milk\n\n## 11:30:47 (edited)\n\nCall Sam\n- parking\n"
        blocks = grammar.parse_day(raw)
        self.assertEqual([b.time for b in blocks], ["09:14:02", "11:30:47"])
        self.assertEqual(blocks[0].text, "Buy oat milk")
        self.assertFalse(blocks[0].edited)
        self.assertEqual(blocks[1].text, "Call Sam\n- parking")
        self.assertTrue(blocks[1].edited)
        self.assertEqual("".join(b.raw for b in blocks), raw)

    def test_attachments_and_caption(self):
        raw = ("## 14:23:05\n\n![IMG 2034.png](attachments/2026-10-04_142305_IMG-2034.png)\n"
               "[Lease draft.pdf](attachments/2026-10-04_142305_Lease-draft.pdf)\n\nPhotos\n")
        b = grammar.parse_day(raw)[0]
        self.assertEqual(b.text, "Photos")
        self.assertEqual([a.file for a in b.atts],
                         ["2026-10-04_142305_IMG-2034.png", "2026-10-04_142305_Lease-draft.pdf"])
        self.assertEqual(b.atts[0].name, "IMG 2034.png")

    def test_tolerant_headings(self):
        raw = "##  9:05\n\nshort\n## 23:59:59 (Edited)\nx\n## 25:00\nnot a heading\n## 12:00 standup\n"
        blocks = grammar.parse_day(raw)
        self.assertEqual([b.time for b in blocks], ["09:05:00", "23:59:59"])
        self.assertTrue(blocks[1].edited)
        self.assertEqual(blocks[1].text, "x\n## 25:00\nnot a heading\n## 12:00 standup")

    def test_preamble_crlf_bom(self):
        raw = "﻿hello there\r\n\r\n## 10:00:00\r\n\r\nworld\r\n"
        blocks = grammar.parse_day(raw)
        self.assertEqual(len(blocks), 2)
        self.assertFalse(blocks[0].has_heading)
        self.assertEqual(blocks[0].time, "00:00:00")
        self.assertEqual(blocks[0].text, "hello there")
        self.assertEqual(blocks[1].text, "world")
        self.assertEqual("".join(b.raw for b in blocks), raw)

    def test_no_trailing_newline_and_empty(self):
        self.assertEqual(grammar.parse_day(""), [])
        raw = "## 10:00:00\n\nlast line"
        blocks = grammar.parse_day(raw)
        self.assertEqual(blocks[0].text, "last line")
        empty = grammar.parse_day("## 10:00:00\n\n\n## 10:00:01\n\nx\n")
        self.assertFalse(empty[0].visible)
        self.assertTrue(empty[1].visible)

    def test_invalid_utf8_and_u2028_lossless(self):
        data = b"## 10:00:00\n\nbad \xff\xfe bytes \xe2\x80\xa8 line sep\n"
        raw = data.decode("utf-8", "surrogateescape")
        blocks = grammar.parse_day(raw)
        self.assertEqual(len(blocks), 1)
        self.assertEqual("".join(b.raw for b in blocks).encode("utf-8", "surrogateescape"), data)
        self.assertIn(" ", blocks[0].text)

    def test_same_second_ordinals_and_ids(self):
        raw = "## 09:14:02\n\na\n\n## 09:14:02\n\nb\n\n## 09:15:00\n\nc\n"
        blocks = grammar.parse_day(raw)
        self.assertEqual([b.ordinal for b in blocks], [0, 1, 0])
        self.assertEqual(blocks[1].id("2026-10-04"), "2026-10-04T091402-1")
        self.assertEqual(grammar.parse_id("2026-10-04T091402-1"), ("2026-10-04", "09:14:02", 1))
        self.assertIsNone(grammar.parse_id("delete"))

    def test_attachment_name_escapes_and_bad_paths(self):
        a = Att(file="f.png", name="we]ird [na\\me")
        raw = grammar.format_block("10:00:00", "", [a])
        b = grammar.parse_day(raw)[0]
        self.assertEqual(b.atts, [a])
        for line in ("[x](attachments/../x)", "[x](attachments/.hidden)", "[x](attachments/a%2Fb)"):
            b = grammar.parse_day("## 10:00:00\n\n" + line + "\n")[0]
            self.assertEqual(b.atts, [])
            self.assertEqual(b.text, line)
        b = grammar.parse_day("## 10:00:00\n\n[x](<attachments/a%20b.png>)\n")[0]
        self.assertEqual(b.atts[0].file, "a b.png")


class TestEscaping(unittest.TestCase):
    CASES = [
        "## 12:00",
        "\\## 12:00",
        "\\\\## 12:00 (edited)",
        "[not a file](attachments/x.png)",
        "![pic](attachments/y.png)",
        "## 12:00 standup",
        "plain",
    ]

    def test_table(self):
        for line in self.CASES:
            for text in (line, "first\n" + line, line + "\nlast"):
                raw = grammar.format_block("10:00:00", text, [])
                blocks = grammar.parse_day(raw)
                self.assertEqual(len(blocks), 1, (text, raw))
                self.assertEqual(blocks[0].text, text, raw)

    def test_escaped_on_disk(self):
        raw = grammar.format_block("10:00:00", "## 12:00\n## 12:00 standup", [])
        self.assertIn("\n\\## 12:00\n## 12:00 standup\n", raw)

    def test_caption_link_after_attachments(self):
        atts = [Att(file="a.png", name="a.png")]
        text = "[x](attachments/b.png)\nmore"
        b = grammar.parse_day(grammar.format_block("10:00:00", text, atts))[0]
        self.assertEqual((b.text, b.atts), (text, atts))


class TestNormalize(unittest.TestCase):
    def test_rules(self):
        n = grammar.normalize_text
        self.assertEqual(n("  \n\t\n  indented\r\nline  \n\n"), "  indented\nline")
        self.assertEqual(n(" \n \n"), "")
        self.assertEqual(n("a\rb"), "a\nb")
        for t in ("x", " y\n\nz ", "\n\n q"):
            self.assertEqual(n(n(t)), n(t))

    def test_length(self):
        self.assertFalse(grammar.text_too_long("a" * 65536))
        self.assertTrue(grammar.text_too_long("a" * 65537))


class TestResolve(unittest.TestCase):
    def test_resolve(self):
        raw = "## 09:14:02\n\na\n\n## 09:14:02\n\nb\n"
        blocks = grammar.parse_day(raw)
        b = blocks[1]
        self.assertIs(grammar.resolve(blocks, b.id("2026-10-04"), b.hash), b)
        # The first same-second message was deleted externally: position shifted, hash unique.
        shifted = grammar.parse_day("## 09:14:02\n\nb\n")
        self.assertIs(grammar.resolve(shifted, b.id("2026-10-04"), b.hash), shifted[0])
        # Text changed: no match.
        changed = grammar.parse_day("## 09:14:02\n\na\n\n## 09:14:02\n\nB!\n")
        self.assertIsNone(grammar.resolve(changed, b.id("2026-10-04"), b.hash))
        self.assertIsNone(grammar.resolve(blocks, "garbage", b.hash))


ALPHABET = [
    "a", "b", "Z", " ", " ", "\t", "\n", "\n", "\n", "\r", "\r\n", "#", "##", "## ", "\\",
    "[", "]", "(", ")", "!", ":", "1", "2", "9", "0", "12:00", "09:14:02", "attachments/",
    "x.png", "(edited)", "é", " ", "﻿", "😀", "-", ".", "<", ">", "%20", "\x0c",
]
ATT_NAMES = ["a.png", "doc.pdf", "IMG-1.jpg", "x_y.mp4", "file"]


def rand_text(rng):
    n = rng.randint(0, 12)
    return "".join(rng.choice(ALPHABET) for _ in range(n))


def rand_message(rng):
    text = grammar.normalize_text(rand_text(rng))
    atts = []
    if rng.random() < 0.25:
        for _ in range(rng.randint(1, 3)):
            name = rand_text(rng).replace("\r", " ").replace("\n", " ")
            atts.append(Att(file=rng.choice(ATT_NAMES), name=name or "n"))
    if not text and not atts:
        text = "x"
    t = "%02d:%02d:%02d" % (rng.randint(0, 23), rng.choice([0, 0, 59, 14]), rng.choice([0, 2, 59]))
    return t, text, atts


class TestFuzz(unittest.TestCase):
    def test_round_trip_locality_fidelity(self):
        rng = random.Random(20261004)
        for case in range(FUZZ_CASES):
            msgs = [rand_message(rng) for _ in range(rng.randint(1, 4))]
            edited = [rng.random() < 0.2 for _ in msgs]
            parts = [grammar.format_block(t, x, a, e) for (t, x, a), e in zip(msgs, edited)]
            raw = join_blocks(parts)
            blocks = grammar.parse_day(raw)
            # G1
            self.assertEqual("".join(b.raw for b in blocks), raw)
            # G3
            got = [(b.time, b.text, b.atts, b.edited) for b in blocks]
            want = [(t, x, a, e) for (t, x, a), e in zip(msgs, edited)]
            self.assertEqual(got, want, repr(raw))
            # G2: edit one block; every other block's raw is byte-identical
            i = rng.randrange(len(blocks))
            nt, nx, na = rand_message(rng)
            new_raw = grammar.format_block(blocks[i].time, nx, blocks[i].atts, True)
            new_raw += grammar.trailing_blank(blocks[i].raw)
            raws = [b.raw for b in blocks]
            raws[i] = new_raw
            after = grammar.parse_day("".join(raws))
            self.assertEqual(len(after), len(blocks))
            for j, b in enumerate(after):
                if j == i:
                    self.assertEqual((b.text, b.atts, b.edited), (nx if (nx or blocks[i].atts) else "", blocks[i].atts, True))
                else:
                    self.assertEqual(b.raw, blocks[j].raw)
                    self.assertEqual(b.text, blocks[j].text)

    def test_arbitrary_input_is_lossless(self):
        rng = random.Random(7)
        pool = ALPHABET + ["## 10:00:00\n", "\n## 1:2\n", "![a](attachments/b.png)\n"]
        for _ in range(max(1000, FUZZ_CASES // 10)):
            raw = "".join(rng.choice(pool) for _ in range(rng.randint(0, 30)))
            blocks = grammar.parse_day(raw)
            self.assertEqual("".join(b.raw for b in blocks), raw)


if __name__ == "__main__":
    unittest.main()
