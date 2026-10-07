"""On-disk storage: one folder per chat, one Markdown file per local day.

Layout (root defaults to ~/NoteToSelf)::

    <root>/<Chat>/YYYY-MM-DD.md
    <root>/<Chat>/attachments/YYYY-MM-DD_HHMMSS_<name>.<ext>
    <root>/<Chat>/.draft.md      the unsent draft, if any
    <root>/.trash/...            deleted/replaced notes, deleted chats, send journal
    <root>/.notetoself.json      marks the folder as ours; holds the browser access key

Everything the app stores lives under <root>; "Delete all data" removes <root> entirely.

Display order is file order (never re-sorted by time), so DST changes and travel
can't scramble history. Every write is fsynced before it is acknowledged.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import struct
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

from . import grammar
from .grammar import Att, Block

DEFAULT_CHAT = "Note to Self"
TRASH = ".trash"
MARKER = ".notetoself.json"
DRAFT = ".draft.md"
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
# Files the OS drops into folders; they don't make a folder "someone else's".
OS_FILES = {".DS_Store", ".localized", "Thumbs.db", "desktop.ini"}
DAY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\.md$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CONFLICT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}.+\.md$", re.IGNORECASE)
WORD_RE = re.compile(r"\w+")
MAX_UPLOAD = 100 * 1024 * 1024
MAX_ATTACHMENTS = 32
MAX_NAME = 64
CHUNK = 1024 * 1024
STALE_SECONDS = 600
IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"

KIND_BY_EXT = {}
for _ext in ("png", "jpg", "jpeg", "gif", "webp", "avif", "bmp"):
    KIND_BY_EXT[_ext] = "image"
for _ext in ("mp4", "m4v", "webm", "mov"):
    KIND_BY_EXT[_ext] = "video"
for _ext in ("mp3", "m4a", "aac", "wav", "ogg", "opus", "flac"):
    KIND_BY_EXT[_ext] = "audio"

EXT_BY_TYPE = {
    "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp",
    "image/avif": "avif", "image/bmp": "bmp", "image/heic": "heic", "image/svg+xml": "svg",
    "video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov",
    "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/aac": "aac", "audio/wav": "wav",
    "audio/x-wav": "wav", "audio/ogg": "ogg", "audio/flac": "flac",
    "application/pdf": "pdf", "application/zip": "zip", "application/json": "json",
    "text/plain": "txt", "text/markdown": "md", "text/csv": "csv", "text/html": "html",
}

RESERVED = {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(1, 10)} | {
    "LPT%d" % i for i in range(1, 10)}
BAD_NAME_CHARS = set('<>:"/\\|?*')


class FolderError(Exception):
    """The notes folder can't be used (it holds unrelated files, or isn't writable)."""


class ApiError(Exception):
    """An error with an HTTP status and a stable code for the JSON response."""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


# ---------------------------------------------------------------------------
# Durability helpers
# ---------------------------------------------------------------------------

def fsync_fd(fd: int) -> None:
    """fsync; on macOS use F_FULLFSYNC, since plain fsync doesn't flush the drive cache."""
    if IS_MAC:
        try:
            import fcntl
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except (ImportError, AttributeError, OSError):
            pass
    os.fsync(fd)


def fsync_dir(path: str) -> None:
    if IS_WINDOWS:
        return  # Windows can't fsync folders
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def append_durable(path: str, data: bytes) -> None:
    is_new = not os.path.exists(path)
    with open(path, "ab") as f:  # binary: no newline translation on Windows
        f.write(data)
        f.flush()
        fsync_fd(f.fileno())
    if is_new:
        fsync_dir(os.path.dirname(path))


def replace_retry(src: str, dst: str) -> None:
    """os.replace, retrying while Windows has the target locked (editors, indexers, AV)."""
    for attempt in range(10):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if not IS_WINDOWS:
                raise
            time.sleep(0.1)
    raise ApiError(503, "busy", "The file is locked by another program. Try again.")


def atomic_write(path: str, data: bytes, mode: Optional[int] = None) -> None:
    """Write via temp file + fsync + rename. `mode` (e.g. 0o600) is for new private files."""
    d, name = os.path.split(path)
    tmp = os.path.join(d, ".%s.%s.tmp" % (name, uuid.uuid4().hex[:12]))
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                     0o666 if mode is None else mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            fsync_fd(f.fileno())
        if mode is None and os.path.exists(path):
            try:
                shutil.copymode(path, tmp)
            except OSError:
                pass
        replace_retry(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    fsync_dir(d)


def remove_tree(path: str) -> None:
    """Delete a folder and everything in it. Never follows symlinks out of it (a symlink is
    removed, not its target); read-only files (Windows) are made writable first."""
    def retry(func: Any, p: str, exc: BaseException) -> None:
        if isinstance(exc, FileNotFoundError):
            return
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD | (stat.S_IEXEC if os.path.isdir(p) else 0))
        except OSError:
            pass
        func(p)

    if os.path.islink(path) or os.path.isfile(path):
        os.remove(path)
    elif os.path.isdir(path):
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=retry)
        else:
            shutil.rmtree(path, onerror=lambda func, p, info: retry(func, p, info[1]))


def rename_retry(src: str, dst: str) -> None:
    for attempt in range(10):
        try:
            os.rename(src, dst)
            return
        except PermissionError:
            if not IS_WINDOWS:
                raise
            time.sleep(0.1)
    raise ApiError(503, "busy", "A file in the folder is locked by another program. Try again.")


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------

def local_ts(date: str, time_str: str) -> int:
    """Epoch milliseconds for a local wall-clock date and time (fold=0 in a repeated hour)."""
    try:
        dt = _dt.datetime.strptime(date + " " + time_str, "%Y-%m-%d %H:%M:%S")
        return int(dt.timestamp() * 1000)
    except (ValueError, OverflowError, OSError):
        return 0


def valid_date(s: Any) -> bool:
    if not isinstance(s, str) or not DATE_RE.match(s):
        return False
    try:
        _dt.date(int(s[:4]), int(s[5:7]), int(s[8:10]))
        return True
    except ValueError:
        return False


def kind_for(file: str) -> str:
    ext = file.rsplit(".", 1)[-1].lower() if "." in file else ""
    return KIND_BY_EXT.get(ext, "file")


def fold(s: str) -> str:
    """Casefold and strip accents, for search."""
    s = unicodedata.normalize("NFKD", s.casefold())
    return "".join(c for c in s if not unicodedata.combining(c))


def tokens(s: str) -> List[str]:
    return WORD_RE.findall(fold(s))


def display_name(name: Any, fallback: str) -> str:
    if not isinstance(name, str):
        return fallback
    name = re.split(r"[/\\]", name)[-1]
    name = unicodedata.normalize("NFC", name)
    name = "".join(" " if unicodedata.category(c) == "Cc" else c for c in name).strip()
    name = name.encode("utf-8", "replace").decode("utf-8")
    return name[:200] or fallback


def sanitize_filename(name: Any, ctype: Optional[str] = None) -> str:
    """A safe on-disk file name: basename only, [\\w.-] characters, short, with an extension."""
    base = re.split(r"[/\\]", name if isinstance(name, str) else "")[-1]
    base = unicodedata.normalize("NFC", base).encode("utf-8", "replace").decode("utf-8")
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    ext = re.sub(r"[^0-9A-Za-z]", "", ext).lower()[:10]
    if not ext and ctype:
        ext = EXT_BY_TYPE.get(ctype.split(";")[0].strip().lower(), "")
    stem = re.sub(r"\s+", "-", stem.strip())
    stem = re.sub(r"[^\w.-]", "", stem)
    stem = re.sub(r"([.-])[.-]+", r"\1", stem)
    stem = stem.strip(".-_")[:80].strip(".-_") or "file"
    return stem + ("." + ext if ext else "")


def validate_chat_name(name: Any, existing: Iterable[str], current: Optional[str] = None) -> str:
    if not isinstance(name, str):
        raise ApiError(400, "invalid_name", "Chat name must be text.")
    name = unicodedata.normalize("NFC", name).strip()
    if not name:
        raise ApiError(400, "invalid_name", "Chat name can't be empty.")
    if len(name) > MAX_NAME:
        raise ApiError(400, "invalid_name", "Chat name is too long (max %d characters)." % MAX_NAME)
    if any(c in BAD_NAME_CHARS or unicodedata.category(c) == "Cc" for c in name):
        raise ApiError(400, "invalid_name", 'Chat names can\'t contain < > : " / \\ | ? * or control characters.')
    if name.startswith("."):
        raise ApiError(400, "invalid_name", "Chat names can't start with a period.")
    if name.endswith(".") or name.endswith(" "):
        raise ApiError(400, "invalid_name", "Chat names can't end with a period.")
    if name.split(".")[0].strip().upper() in RESERVED:
        raise ApiError(400, "invalid_name", "That name is reserved by Windows.")
    key = name.casefold()
    cur = current.casefold() if current else None
    for other in existing:
        o = unicodedata.normalize("NFC", other).casefold()
        if o == key and o != cur:
            raise ApiError(409, "name_taken", "A chat with that name already exists.")
    if key == DEFAULT_CHAT.casefold() and cur != key:
        raise ApiError(409, "name_taken", "A chat with that name already exists.")
    return name


def image_size(path: str) -> Optional[Tuple[int, int]]:
    """(width, height) for PNG, GIF, JPEG (EXIF-rotated), WebP and BMP; None otherwise."""
    try:
        with open(path, "rb") as f:
            head = f.read(32)
            if head.startswith(b"\x89PNG\r\n\x1a\n") and head[12:16] == b"IHDR":
                return struct.unpack(">II", head[16:24])
            if head[:6] in (b"GIF87a", b"GIF89a"):
                return struct.unpack("<HH", head[6:10])
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                return _webp_size(head + f.read(32))
            if head[:2] == b"BM" and len(head) >= 26:
                w, h = struct.unpack("<ii", head[18:26])
                return abs(w), abs(h)
            if head[:2] == b"\xff\xd8":
                return _jpeg_size(f)
    except (OSError, struct.error):
        return None
    return None


def _webp_size(b: bytes) -> Optional[Tuple[int, int]]:
    chunk = b[12:16]
    if chunk == b"VP8 " and len(b) >= 30:
        w, h = struct.unpack("<HH", b[26:30])
        return w & 0x3FFF, h & 0x3FFF
    if chunk == b"VP8L" and len(b) >= 25:
        bits = struct.unpack("<I", b[21:25])[0]
        return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
    if chunk == b"VP8X" and len(b) >= 30:
        w = 1 + int.from_bytes(b[24:27], "little")
        h = 1 + int.from_bytes(b[27:30], "little")
        return w, h
    return None


def _jpeg_size(f: Any) -> Optional[Tuple[int, int]]:
    f.seek(2)
    orientation = 1
    for _ in range(10000):
        b = f.read(1)
        while b and b != b"\xff":
            b = f.read(1)
        while b == b"\xff":
            b = f.read(1)
        if not b:
            return None
        marker = b[0]
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            continue
        if marker in (0xD9, 0xDA):
            return None
        lb = f.read(2)
        if len(lb) < 2:
            return None
        seglen = struct.unpack(">H", lb)[0]
        if seglen < 2:
            return None
        if marker == 0xE1 and orientation == 1:
            orientation = _exif_orientation(f.read(seglen - 2)) or 1
            continue
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            data = f.read(5)
            if len(data) < 5:
                return None
            h, w = struct.unpack(">HH", data[1:5])
            return (h, w) if orientation in (5, 6, 7, 8) else (w, h)
        f.seek(seglen - 2, 1)
    return None


def _exif_orientation(data: bytes) -> Optional[int]:
    if not data.startswith(b"Exif\x00\x00") or len(data) < 14:
        return None
    t = data[6:]
    if t[:2] == b"II":
        e = "<"
    elif t[:2] == b"MM":
        e = ">"
    else:
        return None
    try:
        off = struct.unpack(e + "I", t[4:8])[0]
        count = struct.unpack(e + "H", t[off:off + 2])[0]
        for i in range(count):
            p = off + 2 + i * 12
            tag = struct.unpack(e + "H", t[p:p + 2])[0]
            if tag == 0x0112:
                v = struct.unpack(e + "H", t[p + 8:p + 10])[0]
                return v if 1 <= v <= 8 else None
    except struct.error:
        return None
    return None


def snippet(text: str, query: List[str], names: Iterable[str] = ()) -> List[List[Any]]:
    """Up to 10 words starting 2 before the first hit, as [[text, isMatch], ...] segments."""
    src = text if text else " ".join(names)
    flat = re.sub(r"\s+", " ", src).strip()
    words = list(WORD_RE.finditer(flat))
    if not words:
        return [[flat[:80], False]]

    def hit(w: str) -> bool:
        return any(ft.startswith(q) for ft in tokens(w) for q in query)

    hits = [i for i, m in enumerate(words) if hit(m.group())]
    first = hits[0] if hits else 0
    start = max(0, first - 2)
    end = min(len(words), start + 10)
    segs: List[List[Any]] = []

    def add(s: str, is_match: bool) -> None:
        if not s:
            return
        if segs and segs[-1][1] == is_match:
            segs[-1][0] += s
        else:
            segs.append([s, is_match])

    hitset = set(hits)
    if start > 0:
        add("...", False)
        pos = words[start].start()
    else:
        pos = 0
    for i in range(start, end):
        m = words[i]
        add(flat[pos:m.start()], False)
        add(m.group(), i in hitset)
        pos = m.end()
    if end < len(words):
        add("...", False)
    else:
        add(flat[pos:], False)
    return segs


def _file_manager_dbus(path: str, select: bool) -> List[List[str]]:
    """Commands asking the desktop's file manager (Nautilus, Dolphin, Nemo, ...) to show a
    folder, or to open a file's folder with the file selected (org.freedesktop.FileManager1)."""
    method = "org.freedesktop.FileManager1." + ("ShowItems" if select else "ShowFolders")
    uri = "file://" + quote(os.fsencode(os.path.abspath(path)))  # escapes ' and , too
    return [
        ["gdbus", "call", "--session", "--dest", "org.freedesktop.FileManager1",
         "--object-path", "/org/freedesktop/FileManager1", "--method", method, "['%s']" % uri, '""'],
        ["dbus-send", "--session", "--print-reply", "--dest=org.freedesktop.FileManager1",
         "--type=method_call", "/org/freedesktop/FileManager1", method, "array:string:" + uri, "string:"],
    ]


def _open_in_file_manager(path: str, select: bool) -> None:
    quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if IS_MAC:
        cmd = ["open", "-R", path] if select else ["open", path]
    elif IS_WINDOWS:
        cmd = ["explorer", "/select," + path] if select else ["explorer", path]
    else:
        for dbus in _file_manager_dbus(path, select):
            try:
                p = subprocess.Popen(dbus, **quiet)
            except OSError:
                continue  # tool not installed; try the next one
            try:
                if p.wait(timeout=3) == 0:
                    return
            except subprocess.TimeoutExpired:
                return  # the file manager is still starting up; it will show the item
            break  # no FileManager1 service on this desktop: fall back to xdg-open
        cmd = ["xdg-open", os.path.dirname(path) if select else path]
    try:
        subprocess.Popen(cmd, **quiet)
    except OSError:
        raise ApiError(500, "unsupported", "Couldn't open the file manager.")


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

DayFile = Tuple[str, os.stat_result]  # (date, stat)


def _read_marker(root: str) -> Dict[str, Any]:
    try:
        with open(os.path.join(root, MARKER), "rb") as f:
            data = json.loads(f.read(65536).decode("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def folder_key(root: str) -> Optional[str]:
    """The browser access key saved in a notes folder, if it has a valid one."""
    key = _read_marker(root).get("key")
    return key if isinstance(key, str) and KEY_RE.match(key) else None


def folder_settings(root: str) -> Dict[str, Any]:
    """Preferences saved in a notes folder from Settings, such as {"browser": "firefox"}."""
    settings = _read_marker(root).get("settings")
    return dict(settings) if isinstance(settings, dict) else {}


def is_notes_folder(path: str) -> bool:
    """True if `path` is missing, empty, or already a Note to Self folder. Anything else holds
    someone else's files, and we must never list them as chats or delete them."""
    try:
        names = os.listdir(path)
    except FileNotFoundError:
        return True
    except NotADirectoryError:
        return False
    if MARKER in names or any(unicodedata.normalize("NFC", n) == DEFAULT_CHAT for n in names):
        return True
    return all(n in OS_FILES for n in names)


class Store:
    def __init__(self, root: str, warn: Any = None, warm: bool = True) -> None:
        self.root = os.path.abspath(os.path.expanduser(root))
        self.lock = threading.RLock()
        self.warn = warn or (lambda msg: print(msg, file=sys.stderr))
        self._cache: Dict[str, Tuple[tuple, List[Block]]] = {}
        self._img_cache: Dict[str, Tuple[tuple, Optional[Tuple[int, int]]]] = {}
        self._draft_cache: Dict[str, Tuple[tuple, Optional[Dict[str, Any]]]] = {}
        self._warned: set = set()
        if not is_notes_folder(self.root):
            raise FolderError(
                "%s already has other files in it. Note to Self keeps its notes in a folder of "
                "its own: choose a new or empty one with --dir." % self.root)
        self.key = self._read_key()
        fresh = self.key is None
        if fresh:
            self.key = secrets.token_urlsafe(32)
        try:
            self._ensure_dir(os.path.join(self.root, DEFAULT_CHAT))
            if fresh and self._read_key() != self.key:  # the marker existed but was damaged
                self._write_marker()
        except OSError as e:
            raise FolderError("Can't write to %s: %s" % (self.root, e.strerror or e))
        self._purge_stale()
        if warm:
            threading.Thread(target=self._warm, name="nts-warm", daemon=True).start()

    # -- the folder and its key --------------------------------------------------

    def _read_key(self) -> Optional[str]:
        return folder_key(self.root)

    def _write_marker(self, settings: Optional[Dict[str, Any]] = None) -> None:
        body: Dict[str, Any] = {
            "app": "note-to-self",
            "about": ("This folder holds your Note to Self notes. This file marks it as "
                      "Note to Self's and holds the key that lets your browser open the app; "
                      "keep it private."),
            "key": self.key,
        }
        settings = folder_settings(self.root) if settings is None else settings
        if settings:
            body["settings"] = settings
        atomic_write(os.path.join(self.root, MARKER),
                     (json.dumps(body, indent=2) + "\n").encode("utf-8"), mode=0o600)

    def settings(self) -> Dict[str, Any]:
        return folder_settings(self.root)

    def set_settings(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        """Save preferences (a value of None removes one) next to the key."""
        with self.lock:
            settings = self.settings()
            for name, value in changes.items():
                if value is None:
                    settings.pop(name, None)
                else:
                    settings[name] = value
            self._ensure_dir(self.root)
            self._write_marker(settings)
            return settings

    def _ensure_dir(self, path: str) -> None:
        """Create the notes folder (private to this user), its marker file, and `path` in it.
        Only writes create folders, so after "Delete all data" nothing reappears by itself."""
        if not os.path.isdir(self.root):
            parent = os.path.dirname(self.root)
            os.makedirs(parent, exist_ok=True)
            try:
                os.mkdir(self.root, 0o700)  # other accounts on this computer can't look inside
            except FileExistsError:
                pass
            fsync_dir(parent)
        if not os.path.exists(os.path.join(self.root, MARKER)):
            self._write_marker()
        if path != self.root and not os.path.isdir(path):
            os.makedirs(path, exist_ok=True)
            fsync_dir(os.path.dirname(path))

    # -- internals ----------------------------------------------------------

    def _purge_stale(self) -> None:
        """Remove leftover temp files from a crash (only old ones, in case of a twin process)."""
        cutoff = time.time() - STALE_SECONDS
        for d, _dirs, files in os.walk(self.root):
            for name in files:
                if name.startswith(".") and (name.endswith(".tmp") or name.endswith(".part")):
                    p = os.path.join(d, name)
                    try:
                        if os.stat(p).st_mtime < cutoff:
                            os.remove(p)
                    except OSError:
                        pass

    def _warm(self) -> None:
        try:
            for name in self._chat_names():
                cdir = os.path.join(self.root, name)
                for date, st in self._day_files(cdir):
                    self._blocks(os.path.join(cdir, date + ".md"), st)
        except Exception:
            pass

    def _chat_names(self) -> List[str]:
        names = []
        try:
            with os.scandir(self.root) as it:
                for e in it:
                    if e.name.startswith(".") or not e.is_dir():
                        continue
                    names.append(e.name)
        except FileNotFoundError:
            pass
        return names

    def _chat_dir(self, chat: Any) -> str:
        if (not isinstance(chat, str) or not chat or chat.startswith(".")
                or any(c in chat for c in "/\\\0")):
            raise ApiError(404, "not_found", "No such chat.")
        p = os.path.join(self.root, chat)
        if os.path.isdir(p):
            return p
        want = unicodedata.normalize("NFC", chat)
        for name in self._chat_names():
            if unicodedata.normalize("NFC", name) == want:
                return os.path.join(self.root, name)
        if want == DEFAULT_CHAT:  # always exists; its folder is created by the first write
            return os.path.join(self.root, DEFAULT_CHAT)
        raise ApiError(404, "not_found", "No such chat.")

    def _day_files(self, cdir: str) -> List[DayFile]:
        out = []
        try:
            it = os.scandir(cdir)
        except FileNotFoundError:
            if cdir == os.path.join(self.root, DEFAULT_CHAT):
                return []
            raise ApiError(404, "not_found", "No such chat.")
        with it:
            for e in it:
                m = DAY_RE.match(e.name)
                if not m:
                    if CONFLICT_RE.match(e.name) and not e.name.startswith("."):
                        key = os.path.join(cdir, e.name)
                        if key not in self._warned:
                            self._warned.add(key)
                            self.warn("Note: ignoring %s (looks like a sync-conflict copy; "
                                      "merge it into the matching day file by hand)." % key)
                    continue
                if not valid_date(e.name[:10]):
                    continue
                try:
                    if not e.is_file():
                        continue
                    out.append((e.name[:10], e.stat()))
                except OSError:
                    continue
        out.sort(key=lambda x: x[0])
        return out

    @staticmethod
    def _rev_of(files: List[DayFile]) -> str:
        h = hashlib.blake2b(digest_size=8)
        for date, st in files:
            h.update(("%s:%d:%d:%d:%d;" % (date, st.st_mtime_ns, st.st_size, st.st_ino,
                                           st.st_ctime_ns)).encode())
        return h.hexdigest()

    def _rev(self, cdir: str) -> str:
        return self._rev_of(self._day_files(cdir))

    def _read(self, path: str) -> str:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "surrogateescape")

    def _blocks(self, path: str, st: Optional[os.stat_result] = None,
                fresh: bool = False) -> List[Block]:
        if fresh:
            try:
                blocks = grammar.parse_day(self._read(path))
            except FileNotFoundError:
                blocks = []
            self._cache.pop(path, None)
            return blocks
        if st is None:
            try:
                st = os.stat(path)
            except FileNotFoundError:
                return []
        key = (st.st_mtime_ns, st.st_size, st.st_ino, st.st_ctime_ns)
        hit = self._cache.get(path)
        if hit and hit[0] == key:
            return hit[1]
        try:
            blocks = grammar.parse_day(self._read(path))
        except FileNotFoundError:
            return []
        self._cache[path] = (key, blocks)
        return blocks

    def _image_dims(self, path: str, st: os.stat_result) -> Optional[Tuple[int, int]]:
        key = (st.st_mtime_ns, st.st_size)
        hit = self._img_cache.get(path)
        if hit and hit[0] == key:
            return hit[1]
        dims = image_size(path)
        self._img_cache[path] = (key, dims)
        return dims

    def _att_dict(self, chat: str, cdir: str, a: Att) -> Dict[str, Any]:
        kind = kind_for(a.file)
        d: Dict[str, Any] = {
            "file": a.file, "name": a.name, "kind": kind, "size": None,
            "url": "/files/%s/attachments/%s" % (quote(chat, safe=""), quote(a.file, safe="")),
        }
        p = os.path.join(cdir, "attachments", a.file)
        try:
            st = os.stat(p)
            d["size"] = st.st_size
            if kind == "image":
                dims = self._image_dims(p, st)
                if dims:
                    d["w"], d["h"] = dims
        except OSError:
            pass
        return d

    def _message(self, chat: str, cdir: str, date: str, b: Block) -> Dict[str, Any]:
        return {
            "id": b.id(date), "hash": b.hash, "date": date, "time": b.time,
            "ts": local_ts(date, b.time), "edited": b.edited, "text": b.text,
            "attachments": [self._att_dict(chat, cdir, a) for a in b.atts],
        }

    def _day(self, chat: str, cdir: str, date: str, blocks: List[Block]) -> Dict[str, Any]:
        return {"date": date,
                "messages": [self._message(chat, cdir, date, b) for b in blocks if b.visible]}

    def _display(self, cdir: str) -> str:
        return unicodedata.normalize("NFC", os.path.basename(cdir))

    def _chat_info(self, cdir: str) -> Dict[str, Any]:
        name = self._display(cdir)
        files = self._day_files(cdir)
        last = None
        last_ts = None
        for date, st in reversed(files):
            vis = [b for b in self._blocks(os.path.join(cdir, date + ".md"), st) if b.visible]
            if vis:
                b = vis[-1]
                last_ts = local_ts(date, b.time)
                last = {"text": b.text[:200],
                        "attachments": [{"kind": kind_for(a.file), "name": a.name} for a in b.atts]}
                break
        try:
            mtime = int(os.stat(cdir).st_mtime * 1000)
        except OSError:
            mtime = 0
        return {"name": name, "isDefault": name == DEFAULT_CHAT, "rev": self._rev_of(files),
                "lastTs": last_ts, "last": last, "draft": self._draft(cdir),
                "_sort": last_ts if last_ts is not None else mtime}

    def _trash_dir(self, *parts: str) -> str:
        d = os.path.join(self.root, TRASH, *parts)
        os.makedirs(d, exist_ok=True)
        return d

    def _trash_blocks(self, chat_dir: str, date: str, blocks: List[Block], reason: str) -> None:
        d = self._trash_dir(os.path.basename(chat_dir))
        stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        out = []
        for b in blocks:
            out.append("<!-- %s %s -->\n%s\n\n" % (reason, stamp, b.raw.rstrip("\r\n")))
        append_durable(os.path.join(d, date + ".md"), "".join(out).encode("utf-8", "surrogateescape"))

    def _journal(self, chat: str, date: str, block: str) -> None:
        d = self._trash_dir("sent")
        entry = "<!-- %s / %s -->\n%s\n" % (chat, date, block)
        append_durable(os.path.join(d, date[:7] + ".md"), entry.encode("utf-8", "surrogateescape"))

    def _unique(self, path: str) -> str:
        if not os.path.exists(path):
            return path
        stem, ext = os.path.splitext(path)
        n = 1
        while os.path.exists("%s-%d%s" % (stem, n, ext)):
            n += 1
        return "%s-%d%s" % (stem, n, ext)

    def _check_atts(self, cdir: str, atts: Any) -> List[Att]:
        if atts is None:
            return []
        if not isinstance(atts, list):
            raise ApiError(400, "bad_request", "attachments must be a list.")
        if len(atts) > MAX_ATTACHMENTS:
            raise ApiError(400, "bad_request", "Too many attachments (max %d)." % MAX_ATTACHMENTS)
        out = []
        for a in atts:
            if not isinstance(a, dict):
                raise ApiError(400, "bad_request", "Each attachment must be an object.")
            f = a.get("file")
            if not isinstance(f, str) or grammar.safe_file(f) != f or "%" in f:
                raise ApiError(400, "bad_request", "Bad attachment file name.")
            p = os.path.join(cdir, "attachments", f)
            if not os.path.isfile(p):
                raise ApiError(400, "bad_request", "Attachment %s wasn't uploaded." % f)
            out.append(Att(file=f, name=display_name(a.get("name"), f)))
        return out

    @staticmethod
    def _clean_text(text: Any) -> str:
        if text is None:
            text = ""
        if not isinstance(text, str):
            raise ApiError(400, "bad_request", "text must be a string.")
        text = text.encode("utf-8", "replace").decode("utf-8")  # drop lone surrogates
        text = grammar.normalize_text(text)
        if grammar.text_too_long(text):
            raise ApiError(413, "too_long", "Message body is too long.")
        return text

    # -- chats ----------------------------------------------------------------

    def list_chats(self) -> List[Dict[str, Any]]:
        infos = []
        for name in self._chat_names():
            try:
                infos.append(self._chat_info(os.path.join(self.root, name)))
            except ApiError:
                continue
        if not any(c["isDefault"] for c in infos):
            infos.append(self._chat_info(os.path.join(self.root, DEFAULT_CHAT)))
        infos.sort(key=lambda c: (-c["_sort"], not c["isDefault"], c["name"].casefold()))
        for c in infos:
            del c["_sort"]
        return infos

    def chat(self, chat: str) -> Dict[str, Any]:
        info = self._chat_info(self._chat_dir(chat))
        del info["_sort"]
        return info

    def create_chat(self, name: Any) -> Dict[str, Any]:
        with self.lock:
            name = validate_chat_name(name, self._chat_names())
            self._ensure_dir(self.root)
            cdir = os.path.join(self.root, name)
            os.mkdir(cdir)
            fsync_dir(self.root)
            return self.chat(name)

    def rename_chat(self, chat: str, name: Any) -> Dict[str, Any]:
        with self.lock:
            cdir = self._chat_dir(chat)
            current = os.path.basename(cdir)
            if unicodedata.normalize("NFC", current) == DEFAULT_CHAT:
                raise ApiError(403, "default_chat", "Note to Self can't be renamed.")
            name = validate_chat_name(name, self._chat_names(), current=current)
            if name != current:
                rename_retry(cdir, os.path.join(self.root, name))
                fsync_dir(self.root)
                self._cache.clear()
            return self.chat(name)

    def delete_chat(self, chat: str) -> None:
        with self.lock:
            cdir = self._chat_dir(chat)
            name = os.path.basename(cdir)
            if unicodedata.normalize("NFC", name) == DEFAULT_CHAT:
                raise ApiError(403, "default_chat", "Note to Self can't be deleted.")
            dest_dir = self._trash_dir("chats")
            stamp = _dt.datetime.now().strftime("%Y-%m-%d %H%M%S")
            dest = self._unique(os.path.join(dest_dir, "%s %s" % (name, stamp)))
            rename_retry(cdir, dest)
            fsync_dir(self.root)
            fsync_dir(dest_dir)
            self._cache.clear()

    # -- drafts -----------------------------------------------------------------

    def _draft(self, cdir: str) -> Optional[Dict[str, Any]]:
        path = os.path.join(cdir, DRAFT)
        try:
            st = os.stat(path)
        except OSError:
            self._draft_cache.pop(path, None)
            return None
        key = (st.st_mtime_ns, st.st_size, st.st_ino)
        hit = self._draft_cache.get(path)
        if hit and hit[0] == key:
            return hit[1]
        try:
            text = self._read(path).replace("\r\n", "\n")
        except OSError:
            return None
        draft = {"text": text, "ts": st.st_mtime_ns // 1_000_000} if text.strip() else None
        self._draft_cache[path] = (key, draft)
        return draft

    def set_draft(self, chat: str, text: Any) -> Dict[str, Any]:
        """Save the unsent text of a chat (in <chat>/.draft.md), or remove it when blank."""
        cdir = self._chat_dir(chat)
        if text is None:
            text = ""
        if not isinstance(text, str):
            raise ApiError(400, "bad_request", "text must be a string.")
        text = text.encode("utf-8", "replace").decode("utf-8")
        if grammar.text_too_long(text):
            raise ApiError(413, "too_long", "Message body is too long.")
        path = os.path.join(cdir, DRAFT)
        with self.lock:
            if not text.strip():
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
                self._draft_cache.pop(path, None)
                return {"draft": None}
            self._ensure_dir(cdir)
            atomic_write(path, text.encode("utf-8"))
            return {"draft": self._draft(cdir)}

    # -- messages ---------------------------------------------------------------

    def page(self, chat: str, before: Optional[str] = None, after: Optional[str] = None,
             around: Optional[str] = None, frm: Optional[str] = None, to: Optional[str] = None,
             limit: Any = 100) -> Dict[str, Any]:
        for v in (before, after, around, frm, to):
            if v is not None and not valid_date(v):
                raise ApiError(400, "bad_request", "Dates must look like YYYY-MM-DD.")
        try:
            limit = max(1, min(1000, int(limit)))
        except (TypeError, ValueError):
            raise ApiError(400, "bad_request", "limit must be a number.")
        cdir = self._chat_dir(chat)
        files = self._day_files(cdir)
        rev = self._rev_of(files)
        vis: List[Tuple[str, List[Block]]] = []
        for date, st in files:
            blocks = [b for b in self._blocks(os.path.join(cdir, date + ".md"), st) if b.visible]
            if blocks:
                vis.append((date, blocks))
        dates = [d for d, _ in vis]

        def take_back(items: List[Tuple[str, List[Block]]], want: int) -> int:
            """How many items from the end are needed to reach ``want`` messages."""
            n = total = 0
            for _, bl in reversed(items):
                if total >= want:
                    break
                n += 1
                total += len(bl)
            return n

        def take_fwd(items: List[Tuple[str, List[Block]]], want: int) -> int:
            n = total = 0
            for _, bl in items:
                if total >= want:
                    break
                n += 1
                total += len(bl)
            return n

        if frm is not None or to is not None:
            lo, hi = frm or "0000-00-00", to or "9999-99-99"
            sel = [x for x in vis if lo <= x[0] <= hi]
            has_before = any(d < lo for d in dates)
            has_after = any(d > hi for d in dates)
        elif around is not None:
            if not vis:
                sel, has_before, has_after = [], False, False
            else:
                target = _dt.date.fromisoformat(around)
                center = min(range(len(dates)), key=lambda i: (
                    abs((_dt.date.fromisoformat(dates[i]) - target).days), dates[i] > around))
                left, right = vis[:center], vis[center + 1:]
                half = max(1, limit // 2)
                nl, nr = take_back(left, half), take_fwd(right, half)
                sel = left[len(left) - nl:] + [vis[center]] + right[:nr]
                has_before = nl < len(left)
                has_after = nr < len(right)
        elif after is not None:
            items = [x for x in vis if x[0] > after]
            n = take_fwd(items, limit)
            sel = items[:n]
            has_after = n < len(items)
            has_before = any(d <= after for d in dates)
        else:
            cut = before or "9999-99-99"
            items = [x for x in vis if x[0] < cut]
            n = take_back(items, limit)
            sel = items[len(items) - n:]
            has_before = n < len(items)
            has_after = any(d >= cut for d in dates)
        return {"days": [self._day(chat, cdir, d, bl) for d, bl in sel],
                "hasBefore": has_before, "hasAfter": has_after, "rev": rev}

    def send(self, chat: str, text: Any, atts: Any = None,
             now: Optional[_dt.datetime] = None) -> Dict[str, Any]:
        cdir = self._chat_dir(chat)
        text = self._clean_text(text)
        att_list = self._check_atts(cdir, atts)
        if not text and not att_list:
            raise ApiError(400, "empty", "Message is empty.")
        now = now or _dt.datetime.now()
        date, t = now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S")
        name = self._display(cdir)
        block = grammar.format_block(t, text, att_list)
        with self.lock:
            self._ensure_dir(cdir)
            before = self._rev(cdir)
            path = os.path.join(cdir, date + ".md")
            tail = b""
            try:
                with open(path, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    f.seek(max(0, size - 4))
                    tail = f.read()
            except FileNotFoundError:
                pass
            data = grammar.separator(tail) + block
            append_durable(path, data.encode("utf-8", "surrogateescape"))
            try:
                self._journal(name, date, block)
            except OSError as e:
                self.warn("Warning: couldn't write the send journal: %s" % e)
            blocks = self._blocks(path, fresh=True)
            mine = blocks[-1] if blocks else None
            if mine is None or mine.time != t:
                raise ApiError(500, "internal", "The note was written but couldn't be read back.")
            after = self._rev(cdir)
            return {"message": self._message(name, cdir, date, mine),
                    "rev": {"before": before, "after": after}}

    def _conflict(self, chat: str, cdir: str, dates: Iterable[str], msg: str) -> ApiError:
        days = []
        for d in sorted(set(dates)):
            days.append(self._day(chat, cdir, d, self._blocks(os.path.join(cdir, d + ".md"), fresh=True)))
        return ApiError(409, "conflict", msg, days=days)

    def edit(self, chat: str, msg_id: str, h: Any, text: Any) -> Dict[str, Any]:
        cdir = self._chat_dir(chat)
        name = self._display(cdir)
        parsed = grammar.parse_id(msg_id)
        if parsed is None or not valid_date(parsed[0]):
            raise ApiError(404, "not_found", "No such message.")
        date = parsed[0]
        text = self._clean_text(text)
        path = os.path.join(cdir, date + ".md")
        with self.lock:
            before = self._rev(cdir)
            blocks = self._blocks(path, fresh=True)
            target = grammar.resolve(blocks, msg_id, h if isinstance(h, str) else "")
            if target is None:
                raise self._conflict(name, cdir, [date], "This note changed on disk.")
            if not text and not target.atts:
                raise ApiError(400, "empty", "Message is empty.")
            idx = blocks.index(target)
            if text == target.text:
                return {"message": self._message(name, cdir, date, target),
                        "days": [self._day(name, cdir, date, blocks)],
                        "rev": {"before": before, "after": before}}
            self._trash_blocks(cdir, date, [target], "replaced")
            new_raw = grammar.format_block(target.time, text, target.atts, edited=True)
            new_raw += grammar.trailing_blank(target.raw)
            raws = [b.raw for b in blocks]
            raws[idx] = new_raw
            atomic_write(path, "".join(raws).encode("utf-8", "surrogateescape"))
            blocks = self._blocks(path, fresh=True)
            after = self._rev(cdir)
            return {"message": self._message(name, cdir, date, blocks[idx]),
                    "days": [self._day(name, cdir, date, blocks)],
                    "rev": {"before": before, "after": after}}

    def delete(self, chat: str, items: Any) -> Dict[str, Any]:
        cdir = self._chat_dir(chat)
        name = self._display(cdir)
        if not isinstance(items, list) or not items or len(items) > 1000:
            raise ApiError(400, "bad_request", "items must be a list of 1-1000 messages.")
        by_date: Dict[str, List[Tuple[str, str]]] = {}
        for it in items:
            if not isinstance(it, dict) or not isinstance(it.get("id"), str):
                raise ApiError(400, "bad_request", "Each item needs an id and a hash.")
            p = grammar.parse_id(it["id"])
            if p is None or not valid_date(p[0]):
                raise ApiError(404, "not_found", "No such message.")
            h = it.get("hash") if isinstance(it.get("hash"), str) else ""
            by_date.setdefault(p[0], []).append((it["id"], h))
        with self.lock:
            before = self._rev(cdir)
            plan = []
            for date, pairs in sorted(by_date.items()):
                path = os.path.join(cdir, date + ".md")
                blocks = self._blocks(path, fresh=True)
                targets: List[Block] = []
                for msg_id, h in pairs:
                    b = grammar.resolve(blocks, msg_id, h)
                    if b is None:
                        raise self._conflict(name, cdir, by_date.keys(),
                                             "Some notes changed on disk.")
                    if not any(b is t for t in targets):
                        targets.append(b)
                plan.append((date, path, blocks, targets))
            deleted = 0
            moved: List[str] = []
            for date, path, blocks, targets in plan:
                self._trash_blocks(cdir, date, targets, "deleted")
                keep = [b for b in blocks if not any(b is t for t in targets)]
                raw = "".join(b.raw for b in keep)
                if raw.strip():
                    atomic_write(path, raw.encode("utf-8", "surrogateescape"))
                else:
                    os.remove(path)
                    fsync_dir(cdir)
                self._cache.pop(path, None)
                deleted += len(targets)
                for t in targets:
                    moved.extend(a.file for a in t.atts)
            self._trash_attachments(cdir, moved)
            after = self._rev(cdir)
            days = [self._day(name, cdir, date, self._blocks(path, fresh=True))
                    for date, path, _, _ in plan]
            return {"deleted": deleted, "days": days, "rev": {"before": before, "after": after}}

    def _trash_attachments(self, cdir: str, files: List[str]) -> None:
        if not files:
            return
        still_used = set()
        for date, st in self._day_files(cdir):
            for b in self._blocks(os.path.join(cdir, date + ".md"), st):
                still_used.update(a.file for a in b.atts)
        dest_dir = None
        for f in set(files) - still_used:
            src = os.path.join(cdir, "attachments", f)
            if not os.path.isfile(src):
                continue
            if dest_dir is None:
                dest_dir = self._trash_dir(os.path.basename(cdir), "attachments")
            try:
                rename_retry(src, self._unique(os.path.join(dest_dir, f)))
            except (OSError, ApiError) as e:
                self.warn("Warning: couldn't move %s to the trash: %s" % (src, e))

    # -- attachments ------------------------------------------------------------

    def upload(self, chat: str, name: Any, ctype: Optional[str], rfile: Any,
               length: Optional[int], now: Optional[_dt.datetime] = None) -> Dict[str, Any]:
        cdir = self._chat_dir(chat)
        if length is None:
            raise ApiError(411, "length_required", "Content-Length is required.")
        if length < 0:
            raise ApiError(400, "bad_request", "Bad Content-Length.")
        if length > MAX_UPLOAD:
            raise ApiError(413, "too_large", "Attachments can be at most 100 MB.")
        adir = os.path.join(cdir, "attachments")
        with self.lock:
            self._ensure_dir(adir)
        part = os.path.join(adir, ".%s.part" % uuid.uuid4().hex)
        try:
            remaining = length
            with open(part, "wb") as f:
                while remaining > 0:
                    chunk = rfile.read(min(CHUNK, remaining))
                    if not chunk:
                        raise ApiError(400, "bad_request", "Upload was interrupted.")
                    f.write(chunk)
                    remaining -= len(chunk)
                f.flush()
                fsync_fd(f.fileno())
            now = now or _dt.datetime.now()
            stored = now.strftime("%Y-%m-%d_%H%M%S_") + sanitize_filename(name, ctype)
            with self.lock:
                final = self._unique(os.path.join(adir, stored))
                replace_retry(part, final)
                fsync_dir(adir)
        except BaseException:
            try:
                os.remove(part)
            except OSError:
                pass
            raise
        file = os.path.basename(final)
        return self._att_dict(self._display(cdir), cdir, Att(file=file, name=display_name(name, file)))

    def attachment_path(self, chat: str, file: str) -> str:
        cdir = self._chat_dir(chat)
        if grammar.safe_file(file) is None:
            raise ApiError(404, "not_found", "No such file.")
        base = os.path.realpath(os.path.join(cdir, "attachments"))
        p = os.path.realpath(os.path.join(base, file))
        if not p.startswith(base + os.sep) or not os.path.isfile(p):
            raise ApiError(404, "not_found", "No such file.")
        return p

    # -- search -----------------------------------------------------------------

    def search(self, q: Any, chat: Optional[str] = None) -> Dict[str, Any]:
        query = list(dict.fromkeys(tokens(q if isinstance(q, str) else "")))
        if not query:
            return {"chats": [], "messages": [], "truncated": False}
        cap = 100 if chat else 500
        if chat:
            cdirs = [self._chat_dir(chat)]
            chat_hits: List[Dict[str, Any]] = []
        else:
            cdirs = [os.path.join(self.root, n) for n in self._chat_names()]
            chat_hits = []
            for cd in cdirs:
                nm = self._display(cd)
                blob = " " + " ".join(tokens(nm))
                if all((" " + t) in blob for t in query):
                    chat_hits.append({"name": nm, "isDefault": nm == DEFAULT_CHAT})
            chat_hits.sort(key=lambda c: (not c["isDefault"], c["name"].casefold()))
        by_date: Dict[str, List[Tuple[str, str, os.stat_result]]] = {}
        for cd in cdirs:
            for date, st in self._day_files(cd):
                by_date.setdefault(date, []).append((cd, date, st))
        results: List[Dict[str, Any]] = []
        truncated = False
        for date in sorted(by_date, reverse=True):
            found = []
            for cd, _, st in by_date[date]:
                blocks = self._blocks(os.path.join(cd, date + ".md"), st)
                for idx, b in enumerate(blocks):
                    if not b.visible:
                        continue
                    if b.blob is None:
                        b.blob = " " + " ".join(tokens(b.text + " " + " ".join(a.name for a in b.atts)))
                    if all((" " + t) in b.blob for t in query):
                        found.append((local_ts(date, b.time), idx, cd, b))
            found.sort(key=lambda x: (-x[0], -x[1]))
            for ts, _, cd, b in found:
                if len(results) >= cap:
                    truncated = True
                    break
                results.append({
                    "chat": self._display(cd), "id": b.id(date), "date": date, "time": b.time,
                    "ts": ts, "snippet": snippet(b.text, query, [a.name for a in b.atts]),
                })
            if truncated:
                break
        return {"chats": chat_hits, "messages": results, "truncated": truncated}

    # -- misc -------------------------------------------------------------------

    def reveal(self, chat: Optional[str] = None, date: Optional[str] = None) -> None:
        target, select = self.root, False
        if chat:
            target = self._chat_dir(chat)
            if date:
                if not valid_date(date):
                    raise ApiError(400, "bad_request", "Dates must look like YYYY-MM-DD.")
                p = os.path.join(target, date + ".md")
                if os.path.isfile(p):
                    target, select = p, True
        if not os.path.exists(target):
            raise ApiError(404, "not_found", "That folder doesn't exist yet. It's created "
                                             "when you send your first note.")
        _open_in_file_manager(target, select)

    # -- your data ------------------------------------------------------------------

    def storage(self) -> Dict[str, Any]:
        """What's in the notes folder, for the Settings page."""
        notes = att_count = att_bytes = drafts = 0
        chats = 0
        for name in self._chat_names():
            cdir = os.path.join(self.root, name)
            chats += 1
            for date, st in self._day_files(cdir):
                notes += sum(1 for b in self._blocks(os.path.join(cdir, date + ".md"), st)
                             if b.visible)
            try:
                with os.scandir(os.path.join(cdir, "attachments")) as it:
                    for e in it:
                        if not e.name.startswith(".") and e.is_file(follow_symlinks=False):
                            att_count += 1
                            att_bytes += e.stat(follow_symlinks=False).st_size
            except OSError:
                pass
            if self._draft(cdir):
                drafts += 1
        total = trash_bytes = trash_files = 0
        trash = os.path.join(self.root, TRASH)
        for d, _dirs, files in os.walk(self.root):  # os.walk doesn't follow symlinked folders
            in_trash = d == trash or d.startswith(trash + os.sep)
            for f in files:
                try:
                    size = os.lstat(os.path.join(d, f)).st_size
                except OSError:
                    continue
                total += size
                if in_trash:
                    trash_bytes += size
                    trash_files += 1
        return {"root": self.root, "exists": os.path.isdir(self.root),
                "chats": max(chats, 1), "notes": notes, "drafts": drafts,
                "attachments": {"count": att_count, "bytes": att_bytes},
                "trash": {"files": trash_files, "bytes": trash_bytes}, "totalBytes": total}

    def empty_trash(self) -> Dict[str, Any]:
        """Permanently delete .trash: deleted notes, old versions, deleted chats, the send log."""
        with self.lock:
            try:
                remove_tree(os.path.join(self.root, TRASH))
            except OSError as e:
                raise ApiError(500, "delete_failed", "Couldn't empty the trash: %s" % (e.strerror or e))
        return self.storage()

    def wipe(self) -> None:
        """Delete the whole notes folder (notes, attachments, drafts, trash, marker), so
        nothing Note to Self stored is left on this computer."""
        with self.lock:
            real = os.path.realpath(self.root)
            home = os.path.realpath(os.path.expanduser("~"))
            if real in (os.path.dirname(real), home, os.path.dirname(home)) or not is_notes_folder(real):
                raise ApiError(403, "unsafe", "Refusing to delete %s: it doesn't look like a "
                                              "Note to Self folder." % self.root)
            try:
                remove_tree(real)
                if os.path.islink(self.root):
                    os.remove(self.root)
            except OSError as e:
                raise ApiError(500, "delete_failed", "Couldn't delete everything in %s: %s"
                               % (self.root, e.strerror or e))
            self._cache.clear()
            self._img_cache.clear()
            self._draft_cache.clear()
            if os.path.lexists(self.root):
                raise ApiError(500, "delete_failed", "Couldn't delete %s." % self.root)
            fsync_dir(os.path.dirname(self.root))
