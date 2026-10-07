"""Parse and format day files. Pure functions, no I/O.

A day file holds one chat's messages for one local date, e.g.::

    ## 09:14:02

    Buy oat milk

    ## 14:23:05 (edited)

    ![IMG 2034.png](attachments/2026-10-04_142305_IMG-2034.png)

    Photos from the viewing

Writing is strict; reading is tolerant. Guarantees (see tests/test_grammar.py):

G1  "".join(b.raw for b in parse_day(raw)) == raw, for any input.
G2  Sending only appends; edit/delete replace only the targeted blocks' raw.
G3  parse(format(text, atts)) gives back (normalize_text(text), atts).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from urllib.parse import unquote

HEADING_RE = re.compile(
    r"^##[ \t]+([01]?[0-9]|2[0-3]):([0-5][0-9])(?::([0-5][0-9]))?"
    r"(?:[ \t]+(\((?i:edited)\)))?[ \t]*$"
)
ATTACH_RE = re.compile(
    r"^(!?)\[((?:[^\]\\\n]|\\.)*)\]\(<?attachments/([^<>()\s]+?)>?\)[ \t]*$"
)
ID_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2})(\d{2})(\d{2})-(\d{1,6})$")
MAX_TEXT_BYTES = 65536
_NAME_UNESCAPE_RE = re.compile(r"\\(.)")


@dataclass
class Att:
    """An attachment reference: stored file name (in attachments/) and display name."""

    file: str
    name: str


@dataclass
class Block:
    """One message block. ``raw`` is the exact source text, including trailing blank lines."""

    raw: str
    time: str  # "HH:MM:SS"
    edited: bool
    text: str
    atts: List[Att]
    has_heading: bool
    ordinal: int = 0
    blob: Optional[str] = field(default=None, repr=False, compare=False)  # search cache

    @property
    def visible(self) -> bool:
        return bool(self.text or self.atts)

    def id(self, date: str) -> str:
        return "%sT%s-%d" % (date, self.time.replace(":", ""), self.ordinal)

    @property
    def hash(self) -> str:
        return content_hash(self.time, self.text, [a.file for a in self.atts])


def content_hash(time: str, text: str, files: List[str]) -> str:
    data = json.dumps([time, text, files]).encode("ascii")
    return hashlib.blake2b(data, digest_size=6).hexdigest()


def special(line: str) -> bool:
    """True if the line, ignoring leading backslashes, looks like a heading or attachment line."""
    s = line.lstrip("\\")
    return bool(HEADING_RE.match(s) or ATTACH_RE.match(s))


def _split_keep(raw: str) -> List[str]:
    """Split on "\\n" only (never splitlines), keeping each line's newline."""
    parts = raw.split("\n")
    out = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        out.append(parts[-1])
    return out


def _clean(line: str, first: bool) -> str:
    if line.endswith("\n"):
        line = line[:-1]
    if line.endswith("\r"):
        line = line[:-1]
    if first and line.startswith("﻿"):
        line = line[1:]
    return line


def safe_file(name: str) -> Optional[str]:
    """Percent-decode an attachment file name; None if it could escape attachments/."""
    try:
        f = unquote(name, errors="strict")
    except UnicodeDecodeError:
        return None
    if not f or f.startswith(".") or "/" in f or "\\" in f or "\0" in f:
        return None
    return f


def _unescape_name(s: str) -> str:
    return _NAME_UNESCAPE_RE.sub(r"\1", s)


def _escape_name(s: str) -> str:
    s = s.replace("\r", " ").replace("\n", " ")
    return s.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _parse_body(lines: List[str]) -> Tuple[str, List[Att]]:
    i, n = 0, len(lines)
    while i < n and not lines[i].strip():
        i += 1
    atts: List[Att] = []
    while i < n:
        s = lines[i]
        if not s.strip():
            i += 1
            continue
        m = ATTACH_RE.match(s)
        if m:
            f = safe_file(m.group(3))
            if f is not None:
                atts.append(Att(file=f, name=_unescape_name(m.group(2))))
                i += 1
                continue
        break
    caption = []
    for s in lines[i:]:
        if s.startswith("\\") and special(s):
            s = s[1:]
        caption.append(s)
    return "\n".join(caption).rstrip(), atts


def _make_block(head: Optional[re.Match], raw_lines: List[str], clean_lines: List[str]) -> Block:
    if head is None:
        text, atts = _parse_body(clean_lines)
        return Block(raw="".join(raw_lines), time="00:00:00", edited=False,
                     text=text, atts=atts, has_heading=False)
    text, atts = _parse_body(clean_lines[1:])
    time = "%02d:%s:%s" % (int(head.group(1)), head.group(2), head.group(3) or "00")
    return Block(raw="".join(raw_lines), time=time, edited=head.group(4) is not None,
                 text=text, atts=atts, has_heading=True)


def parse_day(raw: str) -> List[Block]:
    """Parse a decoded day file into blocks (lossless: the raws join back to ``raw``)."""
    lines = _split_keep(raw)
    blocks: List[Block] = []
    cur_raw: List[str] = []
    cur_clean: List[str] = []
    cur_head: Optional[re.Match] = None
    for idx, line in enumerate(lines):
        clean = _clean(line, idx == 0)
        m = HEADING_RE.match(clean)
        if m:
            if cur_raw:
                blocks.append(_make_block(cur_head, cur_raw, cur_clean))
            cur_head, cur_raw, cur_clean = m, [line], [clean]
        else:
            cur_raw.append(line)
            cur_clean.append(clean)
    if cur_raw:
        blocks.append(_make_block(cur_head, cur_raw, cur_clean))
    seen: dict = {}
    for b in blocks:
        b.ordinal = seen.get(b.time, 0)
        seen[b.time] = b.ordinal + 1
    return blocks


def normalize_text(t: str) -> str:
    """CRLF/CR -> LF, drop leading whitespace-only lines, strip trailing whitespace."""
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    lines = t.split("\n")
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    return "\n".join(lines[i:]).rstrip()


def text_too_long(t: str) -> bool:
    return len(t.encode("utf-8", "surrogateescape")) > MAX_TEXT_BYTES


def is_image_name(file: str) -> bool:
    ext = file.rsplit(".", 1)[-1].lower() if "." in file else ""
    return ext in ("png", "jpg", "jpeg", "gif", "webp", "avif", "bmp")


def format_block(time: str, text: str, atts: List[Att], edited: bool = False) -> str:
    """Format one message block (ends with a single "\\n"). ``text`` must be normalized."""
    lines = ["## " + time + (" (edited)" if edited else ""), ""]
    for a in atts:
        bang = "!" if is_image_name(a.file) else ""
        lines.append("%s[%s](attachments/%s)" % (bang, _escape_name(a.name), a.file))
    if atts and text:
        lines.append("")
    if text:
        for line in text.split("\n"):
            lines.append("\\" + line if special(line) else line)
    return "\n".join(lines) + "\n"


def separator(tail: bytes) -> str:
    """What to write before appending a block, given the file's last few bytes."""
    if not tail:
        return ""
    if tail.endswith(b"\n\n") or tail.endswith(b"\n\r\n"):
        return ""
    if tail.endswith(b"\n"):
        return "\n"
    return "\n\n"


def trailing_blank(raw: str) -> str:
    """The run of whitespace-only lines at the end of a block's raw (kept on edit)."""
    lines = _split_keep(raw)
    i = len(lines)
    while i > 1 and not lines[i - 1].strip():
        i -= 1
    return "".join(lines[i:])


def parse_id(msg_id: str) -> Optional[Tuple[str, str, int]]:
    m = ID_RE.match(msg_id or "")
    if not m:
        return None
    return m.group(1), "%s:%s:%s" % (m.group(2), m.group(3), m.group(4)), int(m.group(5))


def resolve(blocks: List[Block], msg_id: str, h: str) -> Optional[Block]:
    """Find the block for (id, hash): exact position first, then a unique content match."""
    p = parse_id(msg_id)
    if p is None:
        return None
    _, time, ordinal = p
    for b in blocks:
        if b.visible and b.time == time and b.ordinal == ordinal and b.hash == h:
            return b
    same = [b for b in blocks if b.visible and b.time == time and b.hash == h]
    return same[0] if len(same) == 1 else None
