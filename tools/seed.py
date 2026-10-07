#!/usr/bin/env python3
"""Write demo notes through nts.store (for screenshots and manual testing).

    .venv/bin/python tools/seed.py --out demo-notes --profile readme
    .venv/bin/python tools/seed.py --out /tmp/x --now 2026-10-04T19:00 --spec spec.json

Spec JSON: {"chats": [{"name": "...", "messages": [
    {"minutesAgo": 25, "text": "..."},
    {"at": "2026-10-04T17:52:00", "text": "...", "attachments": [{"path": "/abs/file.png"}]}]}]}
Messages in a chat are written in list order.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import struct
import sys
import tempfile
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nts.store import DEFAULT_CHAT, EXT_BY_TYPE, ApiError, Store  # noqa: E402

TYPE_BY_EXT = {v: k for k, v in EXT_BY_TYPE.items()}


def make_png(w: int = 600, h: int = 400) -> bytes:
    """A small landscape (sky gradient, sun, hills) so README screenshots have a real image."""
    rows = []
    for y in range(h):
        t = y / (h - 1)
        sky = (int(255 - 70 * t), int(170 - 60 * t), int(120 + 60 * t))
        row = bytearray(b"\x00")
        for x in range(w):
            r, g, b = sky
            if (x - 0.68 * w) ** 2 + (y - 0.42 * h) ** 2 < (0.08 * h) ** 2:
                r, g, b = 255, 236, 180
            if y > 0.62 * h + 28 * math.sin(0.011 * x + 1.3):
                r, g, b = 64, 74, 110
            if y > 0.75 * h + 22 * math.sin(0.017 * x + 0.4):
                r, g, b = 38, 46, 74
            row += bytes((r, g, b))
        rows.append(bytes(row))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


def make_pdf(title: str = "Trip itinerary") -> bytes:
    """A tiny valid one-page PDF."""
    text = "BT /F1 24 Tf 72 720 Td (%s) Tj ET" % title
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(text), text.encode()),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def readme_spec(now: dt.datetime, assets: str) -> dict:
    def at(days: int, hh: int, mm: int) -> str:
        d = (now - dt.timedelta(days=days)).replace(hour=hh, minute=mm, second=0, microsecond=0)
        return d.isoformat()

    png = os.path.join(assets, "sunset.png")
    pdf = os.path.join(assets, "Lisbon itinerary.pdf")
    return {"chats": [
        {"name": "Book notes", "messages": [
            {"at": at(4, 20, 15), "text": "Piranesi: the House as a kind of grace. Reread the "
                                          "journal entries about the tides."},
            {"at": at(3, 22, 2), "text": "The Overstory: start part two (Trunk)"},
        ]},
        {"name": "Trip ideas", "messages": [
            {"at": at(2, 12, 30), "text": "Lisbon in May? Check flights and the tram 28 route"},
            {"at": at(1, 9, 5), "text": "", "attachments": [{"path": pdf}]},
            {"at": at(1, 9, 6), "text": "itinerary draft, day 3 still open"},
        ]},
        {"name": "Work log", "messages": [
            {"at": at(0, 9, 40), "text": "shipped the export fix"},
            {"at": at(0, 11, 5), "text": "review the Q4 plan before Thursday"},
        ]},
        {"name": "Groceries", "messages": [
            {"at": at(1, 18, 0), "text": "oat milk"},
            {"minutesAgo": 128, "text": "apples\npears\nbananas\ncoffee beans"},
        ]},
        {"name": DEFAULT_CHAT, "messages": [
            {"at": at(4, 9, 12), "text": "Dentist moved to Thursday 3:30"},
            {"at": at(4, 13, 40), "text": "Book recs from Sam: The Overstory, Piranesi"},
            {"at": at(3, 8, 5), "text": "Ideas for the garden:\n- tomatoes along the fence\n"
                                        "- herbs by the kitchen door\n- move the bench into the shade"},
            {"at": at(3, 18, 22), "text": "renew passport before December"},
            {"at": at(2, 19, 48), "text": "Sunset from the ridge trail", "attachments": [{"path": png}]},
            {"at": at(2, 21, 10), "text": "lentil soup recipe to try this weekend: "
                                          "https://example.com/recipes/lentil-soup"},
            {"at": at(1, 7, 45), "text": "call the bank about the new card"},
            {"at": at(1, 7, 46), "text": "and ask about the travel notice"},
            {"minutesAgo": 25, "text": "Remember: water the plants on Sunday"},
            {"minutesAgo": 14, "text": "Quote I liked: “Simplicity is the ultimate sophistication.”"},
            {"minutesAgo": 7, "text": "standup notes"},
            {"minutesAgo": 6, "text": "move design review to 3pm"},
            {"minutesAgo": 5, "text": "send Alex the slides"},
            {"minutesAgo": 4, "text": "book a bigger room"},
            {"minutesAgo": 2, "text": "order lunch for the team"},
            {"minutesAgo": 1, "text": "and ask about the budget"},
        ]},
    ]}


def apply_spec(store: Store, spec: dict, now: dt.datetime) -> int:
    count = 0
    existing = {c["name"] for c in store.list_chats()}
    for chat in spec.get("chats", []):
        name = chat["name"]
        if name not in existing:
            store.create_chat(name)
            existing.add(name)
        for m in chat.get("messages", []):
            if "at" in m:
                when = dt.datetime.fromisoformat(m["at"])
            else:
                when = now - dt.timedelta(minutes=float(m.get("minutesAgo", 0)))
            atts = []
            for a in m.get("attachments", []):
                path = a["path"]
                ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
                with open(path, "rb") as f:
                    size = os.fstat(f.fileno()).st_size
                    att = store.upload(name, a.get("name") or os.path.basename(path),
                                       TYPE_BY_EXT.get(ext), f, size, now=when)
                atts.append({"file": att["file"], "name": att["name"]})
            store.send(name, m.get("text", ""), atts, now=when)
            count += 1
    return count


def main(argv: list = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", required=True, help="notes folder to write into")
    p.add_argument("--now", help="ISO time treated as 'now' (default: the current time)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--profile", choices=["readme"])
    g.add_argument("--spec", help="JSON spec file")
    args = p.parse_args(argv)
    now = dt.datetime.fromisoformat(args.now) if args.now else dt.datetime.now().replace(microsecond=0)
    out = os.path.abspath(os.path.expanduser(args.out))
    store = Store(out, warm=False)
    if args.spec:
        with open(args.spec, encoding="utf-8") as f:
            spec = json.load(f)
        return _run(store, spec, now, out)
    with tempfile.TemporaryDirectory(prefix="nts-seed-") as assets:
        with open(os.path.join(assets, "sunset.png"), "wb") as f:
            f.write(make_png())
        with open(os.path.join(assets, "Lisbon itinerary.pdf"), "wb") as f:
            f.write(make_pdf("Lisbon itinerary"))
        return _run(store, readme_spec(now, assets), now, out)


def _run(store: Store, spec: dict, now: dt.datetime, out: str) -> int:
    try:
        n = apply_spec(store, spec, now)
    except ApiError as e:
        print("seed failed: %s" % e.message, file=sys.stderr)
        return 1
    print("Wrote %d notes to %s" % (n, out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
