"""HTTP layer: a small framework-free JSON API plus static files, bound to 127.0.0.1.

Every request must carry a Host of 127.0.0.1:<port> or localhost:<port> (blocks DNS
rebinding) and, if the browser sends Sec-Fetch-Site, it must be same-origin or none.
/api requests also need the header ``X-NoteToSelf: 1``: a cross-site page can only send it
after a CORS preflight, and this server never answers preflights (no OPTIONS handler).

Notes are only served to a browser holding the folder's key (other accounts on the same
computer can reach 127.0.0.1 too). The launcher opens ``/?key=...``; the server swaps that
for an HttpOnly, SameSite=Strict cookie and redirects to ``/``.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

from . import __version__, browsers
from .store import DEFAULT_CHAT, ApiError, Store

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
MAX_JSON = 1024 * 1024
API_HEADER = "X-NoteToSelf"
KEY_HEADER = "X-NoteToSelf-Key"  # how the launcher proves it's the same user and folder
COOKIE_AGE = 400 * 24 * 3600  # the longest browsers allow

# Explicit MIME table: never ask the mimetypes module (the Windows registry can map .js to
# text/plain, and Chrome then refuses to run ES modules).
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
}

# Attachment types shown inline; everything else (svg, html, pdf, ...) is downloaded.
INLINE_TYPES = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif",
    "webp": "image/webp", "avif": "image/avif", "bmp": "image/bmp",
    "mp4": "video/mp4", "m4v": "video/mp4", "webm": "video/webm", "mov": "video/quicktime",
    "mp3": "audio/mpeg", "m4a": "audio/mp4", "aac": "audio/aac", "wav": "audio/wav",
    "ogg": "audio/ogg", "opus": "audio/ogg", "flac": "audio/flac",
}

HTML_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; "
            "media-src 'self' blob:; object-src 'none'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'")
FILE_CSP = "sandbox; default-src 'none'; img-src 'self'; media-src 'self'"
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")

# (method, path pattern, handler name); "*" matches one decoded segment.
ROUTES: List[Tuple[str, Tuple[str, ...], str]] = [
    ("GET", ("api", "ping"), "api_ping"),
    ("GET", ("api", "chats"), "api_chats"),
    ("POST", ("api", "chats"), "api_create_chat"),
    ("PATCH", ("api", "chats", "*"), "api_rename_chat"),
    ("DELETE", ("api", "chats", "*"), "api_delete_chat"),
    ("GET", ("api", "chats", "*", "messages"), "api_messages"),
    ("POST", ("api", "chats", "*", "messages"), "api_send"),
    ("POST", ("api", "chats", "*", "messages", "delete"), "api_delete_messages"),
    ("PATCH", ("api", "chats", "*", "messages", "*"), "api_edit"),
    ("POST", ("api", "chats", "*", "attachments"), "api_upload"),
    ("PUT", ("api", "chats", "*", "draft"), "api_draft"),
    ("GET", ("api", "search"), "api_search"),
    ("POST", ("api", "reveal"), "api_reveal"),
    ("GET", ("api", "storage"), "api_storage"),
    ("POST", ("api", "trash", "empty"), "api_empty_trash"),
    ("POST", ("api", "wipe"), "api_wipe"),
    ("POST", ("api", "quit"), "api_quit"),
    ("GET", ("api", "settings"), "api_settings"),
    ("PUT", ("api", "settings"), "api_save_settings"),
    ("POST", ("api", "open"), "api_open_window"),
]
PUBLIC_API = {"api_ping"}  # everything else under /api needs the key


def app_url(port: int, key: str) -> str:
    """The link that opens the app: the address plus the folder's key."""
    return "http://127.0.0.1:%d/?key=%s" % (port, key)


def display_path(path: str) -> str:
    home = os.path.expanduser("~")
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "NoteToSelf/" + __version__
    sys_version = ""
    timeout = 120
    server: "Server"

    # -- plumbing ---------------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:  # quiet
        pass

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    @property
    def store(self) -> Store:
        return self.server.store

    def _dispatch(self, method: str) -> None:
        length = self.headers.get("Content-Length")
        self._body_consumed = (length is None or length.strip() == "0") and \
            self.headers.get("Transfer-Encoding") is None
        try:
            self._check_request()
            split = urlsplit(self.path)
            segs = self._segments(split.path)
            query = dict(parse_qsl(split.query, keep_blank_values=True, errors="strict"))
            if segs and segs[0] == "api":
                if self.headers.get(API_HEADER) != "1":
                    raise ApiError(403, "forbidden", "Missing %s header." % API_HEADER)
                self._route(method, segs, query)
            elif segs and segs[0] == "files" and method in ("GET", "HEAD"):
                self._require_key()
                self._serve_attachment(segs, query, method == "HEAD")
            elif method in ("GET", "HEAD"):
                self._serve_static(segs, query, method == "HEAD")
            else:
                raise ApiError(405, "method_not_allowed", "Method not allowed.")
        except ApiError as e:
            body: Dict[str, Any] = {"error": {"code": e.code, "message": e.message}}
            body.update(e.extra)
            self._send_json(e.status, body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except UnicodeDecodeError:
            self._send_json(400, {"error": {"code": "bad_request", "message": "Bad encoding."}})
        except Exception:
            traceback.print_exc(file=sys.stderr)
            self.close_connection = True
            try:
                self._send_json(500, {"error": {"code": "internal", "message": "Internal error."}})
            except Exception:
                pass

    def _check_request(self) -> None:
        port = self.server.server_address[1]
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in ("127.0.0.1:%d" % port, "localhost:%d" % port):
            raise ApiError(403, "forbidden", "Forbidden host.")
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site not in ("same-origin", "none"):
            raise ApiError(403, "forbidden", "Cross-site requests are not allowed.")

    def _cookie_name(self) -> str:
        return "nts-%d" % self.server.server_address[1]  # cookies are shared by all ports

    def _presented_key(self) -> str:
        got = self.headers.get(KEY_HEADER)
        if got:
            return got.strip()
        name = self._cookie_name()
        for header in self.headers.get_all("Cookie") or []:
            for part in header.split(";"):  # by hand: other apps' odd cookies mustn't break us
                k, sep, v = part.strip().partition("=")
                if sep and k == name:
                    return v.strip().strip('"')
        return ""

    def _authorized(self) -> bool:
        got = self._presented_key()
        return bool(got) and hmac.compare_digest(got.encode("utf-8", "replace"),
                                                 self.store.key.encode("utf-8"))

    def _require_key(self) -> None:
        if not self._authorized():
            raise ApiError(401, "locked", "Open Note to Self with the link that notetoself gives you.")

    @staticmethod
    def _segments(path: str) -> List[str]:
        if not path.startswith("/"):
            raise ApiError(400, "bad_request", "Bad path.")
        out = []
        for raw in path[1:].split("/"):
            seg = unquote(raw, encoding="utf-8", errors="strict")
            if "/" in seg or "\\" in seg or "\0" in seg or seg in (".", ".."):
                raise ApiError(400, "bad_request", "Bad path.")
            out.append(seg)
        return out

    def _common_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Referrer-Policy", "no-referrer")
        if not self._body_consumed:
            self.close_connection = True
        if self.close_connection:
            self.send_header("Connection", "close")

    def _send_json(self, status: int, obj: Any) -> None:
        body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._common_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _content_length(self) -> Optional[int]:
        v = self.headers.get("Content-Length")
        if v is None:
            return None
        try:
            n = int(v)
        except ValueError:
            raise ApiError(400, "bad_request", "Bad Content-Length.")
        if n < 0:
            raise ApiError(400, "bad_request", "Bad Content-Length.")
        return n

    def _read_json(self) -> Dict[str, Any]:
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(411, "length_required", "Content-Length is required.")
        n = self._content_length()
        if n is None:
            raise ApiError(411, "length_required", "Content-Length is required.")
        if n > MAX_JSON:
            raise ApiError(413, "too_large", "Request body is too large.")
        data = self.rfile.read(n) if n else b""
        if len(data) < n:
            raise ApiError(400, "bad_request", "Request body was cut off.")
        self._body_consumed = True
        if not data:
            return {}
        try:
            obj = json.loads(data.decode("utf-8"))
        except ValueError:
            raise ApiError(400, "bad_request", "Request body must be JSON.")
        if not isinstance(obj, dict):
            raise ApiError(400, "bad_request", "Request body must be a JSON object.")
        return obj

    def _route(self, method: str, segs: List[str], query: Dict[str, str]) -> None:
        path_matched = False
        for m, pattern, name in ROUTES:
            if len(pattern) != len(segs):
                continue
            args = []
            ok = True
            for p, s in zip(pattern, segs):
                if p == "*":
                    if not s:
                        ok = False
                        break
                    args.append(s)
                elif p != s:
                    ok = False
                    break
            if not ok:
                continue
            path_matched = True
            if m == method:
                if name not in PUBLIC_API:
                    self._require_key()
                getattr(self, name)(*args, query=query)
                return
        if path_matched:
            raise ApiError(405, "method_not_allowed", "Method not allowed.")
        raise ApiError(404, "not_found", "Not found.")

    # -- API --------------------------------------------------------------------

    def api_ping(self, query: Dict[str, str]) -> None:
        info = {"app": "note-to-self", "version": __version__}
        if self._authorized():  # where the notes are is nobody else's business
            root = self.store.root
            info.update(root=root, rootDisplay=display_path(root), defaultChat=DEFAULT_CHAT,
                        platform=sys.platform)
        self._send_json(200, info)

    def api_chats(self, query: Dict[str, str]) -> None:
        self._send_json(200, {"chats": self.store.list_chats()})

    def api_create_chat(self, query: Dict[str, str]) -> None:
        body = self._read_json()
        self._send_json(201, {"chat": self.store.create_chat(body.get("name"))})

    def api_rename_chat(self, chat: str, query: Dict[str, str]) -> None:
        body = self._read_json()
        self._send_json(200, {"chat": self.store.rename_chat(chat, body.get("name"))})

    def api_delete_chat(self, chat: str, query: Dict[str, str]) -> None:
        self.store.delete_chat(chat)
        self._send_json(200, {"ok": True})

    def api_messages(self, chat: str, query: Dict[str, str]) -> None:
        self._send_json(200, self.store.page(
            chat, before=query.get("before"), after=query.get("after"),
            around=query.get("around"), frm=query.get("from"), to=query.get("to"),
            limit=query.get("limit", 100)))

    def api_send(self, chat: str, query: Dict[str, str]) -> None:
        body = self._read_json()
        self._send_json(201, self.store.send(chat, body.get("text"), body.get("attachments")))

    def api_edit(self, chat: str, msg_id: str, query: Dict[str, str]) -> None:
        body = self._read_json()
        self._send_json(200, self.store.edit(chat, msg_id, body.get("hash"), body.get("text")))

    def api_delete_messages(self, chat: str, query: Dict[str, str]) -> None:
        body = self._read_json()
        self._send_json(200, self.store.delete(chat, body.get("items")))

    def api_upload(self, chat: str, query: Dict[str, str]) -> None:
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(411, "length_required", "Content-Length is required.")
        length = self._content_length()
        att = self.store.upload(chat, query.get("name") or "file",
                                self.headers.get("Content-Type"), self.rfile, length)
        self._body_consumed = True
        self._send_json(201, att)

    def api_draft(self, chat: str, query: Dict[str, str]) -> None:
        body = self._read_json()
        self._send_json(200, self.store.set_draft(chat, body.get("text")))

    def api_search(self, query: Dict[str, str]) -> None:
        self._send_json(200, self.store.search(query.get("q", ""), query.get("chat") or None))

    def api_reveal(self, query: Dict[str, str]) -> None:
        body = self._read_json()
        chat, date = body.get("chat"), body.get("date")
        self.store.reveal(chat if isinstance(chat, str) and chat else None,
                          date if isinstance(date, str) and date else None)
        self._send_json(200, {"ok": True})

    def api_storage(self, query: Dict[str, str]) -> None:
        info = self.store.storage()
        info["rootDisplay"] = display_path(info["root"])
        self._send_json(200, info)

    def api_empty_trash(self, query: Dict[str, str]) -> None:
        self._read_json()
        info = self.store.empty_trash()
        info["rootDisplay"] = display_path(info["root"])
        self._send_json(200, info)

    def api_wipe(self, query: Dict[str, str]) -> None:
        body = self._read_json()
        if body.get("confirm") != "delete all data":
            raise ApiError(400, "bad_request", 'Send {"confirm": "delete all data"} to delete everything.')
        self.store.wipe()
        self.server.wiped = True
        self.close_connection = True
        root = self.store.root
        self._send_json(200, {"ok": True, "root": root, "rootDisplay": display_path(root)})
        # Stop, so nothing writes the folder back. shutdown() blocks, so not on this thread.
        threading.Thread(target=self.server.shutdown, name="nts-stop", daemon=True).start()

    def _settings_info(self) -> Dict[str, Any]:
        auto = browsers.find_browser(browsers.APP_WINDOW)
        return {
            "browser": self.store.settings().get("browser") or "auto",
            "browsers": browsers.installed(),
            "auto": {"id": auto[0], "name": browsers.BROWSERS[auto[0]][0]} if auto else None,
            "env": os.environ.get(browsers.BROWSER_ENV) or None,  # $NOTETOSELF_BROWSER wins
        }

    def api_settings(self, query: Dict[str, str]) -> None:
        self._send_json(200, self._settings_info())

    def api_save_settings(self, query: Dict[str, str]) -> None:
        body = self._read_json()
        if "browser" in body:
            choice = body["browser"]
            if not isinstance(choice, str) or choice.lower() not in browsers.CHOICES:
                raise ApiError(400, "bad_request", "Unknown browser.")
            choice = choice.lower()
            self.store.set_settings({"browser": None if choice == "auto" else choice})
        self._send_json(200, self._settings_info())

    def api_open_window(self, query: Dict[str, str]) -> None:
        """Open another app window, in the browser picked in Settings (used right after
        changing it)."""
        self._read_json()
        choice = os.environ.get(browsers.BROWSER_ENV) or self.store.settings().get("browser")
        url = app_url(self.server.server_address[1], self.store.key)
        opened = browsers.open_window(url, choice, warn=lambda msg: None)
        self._send_json(200, {"ok": bool(opened)})

    def api_quit(self, query: Dict[str, str]) -> None:
        """Quit from the window (or `notetoself --stop`): stop the server."""
        self._read_json()
        with self.store.lock:  # let a write in progress finish first
            self.server.quitting = True
        self.close_connection = True
        self._send_json(200, {"ok": True})
        threading.Thread(target=self.server.shutdown, name="nts-stop", daemon=True).start()

    # -- files ------------------------------------------------------------------

    def _etag(self, st: os.stat_result) -> str:
        return '"%x-%x"' % (st.st_mtime_ns, st.st_size)

    def _not_modified(self, etag: str) -> bool:
        inm = self.headers.get("If-None-Match")
        if not inm:
            return False
        return any(t.strip() in (etag, "W/" + etag, "*") for t in inm.split(","))

    def _send_304(self, etag: str, cache: str = "no-cache") -> None:
        self.send_response(304)
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", cache)
        self._common_headers()
        self.end_headers()

    def _serve_static(self, segs: List[str], query: Dict[str, str], head: bool) -> None:
        if segs in ([], [""]):
            segs = ["index.html"]
        if segs == ["index.html"] and "key" in query:
            self._accept_key(query)
            return
        if any(not s or s.startswith(".") for s in segs):
            raise ApiError(404, "not_found", "Not found.")
        web = os.path.realpath(self.server.web_dir)
        path = os.path.realpath(os.path.join(web, *segs))
        if not path.startswith(web + os.sep) or not os.path.isfile(path):
            raise ApiError(404, "not_found", "Not found.")
        st = os.stat(path)
        etag = self._etag(st)
        if self._not_modified(etag):
            self._send_304(etag)
            return
        ext = os.path.splitext(path)[1].lower()
        ctype = STATIC_TYPES.get(ext, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(st.st_size))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache")
        if ext == ".html":
            self.send_header("Content-Security-Policy", HTML_CSP)
            self.send_header("X-Frame-Options", "DENY")
        self._common_headers()
        self.end_headers()
        if not head:
            self._copy(path, 0, st.st_size)

    def _accept_key(self, query: Dict[str, str]) -> None:
        """/?key=K from the launcher: keep K in a cookie and drop it from the address."""
        key = query.pop("key")
        rest = urlencode(query)
        self.send_response(303)
        self.send_header("Location", "/" + ("?" + rest if rest else ""))
        if hmac.compare_digest(key.encode("utf-8", "replace"), self.store.key.encode("utf-8")):
            self.send_header("Set-Cookie", "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict"
                             % (self._cookie_name(), self.store.key, COOKIE_AGE))
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self._common_headers()
        self.end_headers()

    def _serve_attachment(self, segs: List[str], query: Dict[str, str], head: bool) -> None:
        if len(segs) != 4 or segs[2] != "attachments":
            raise ApiError(404, "not_found", "Not found.")
        path = self.store.attachment_path(segs[1], segs[3])
        st = os.stat(path)
        size = st.st_size
        etag = self._etag(st)
        if self._not_modified(etag):
            self._send_304(etag, "no-store")
            return
        file = segs[3]
        ext = file.rsplit(".", 1)[-1].lower() if "." in file else ""
        ctype = INLINE_TYPES.get(ext)
        if ctype is None:
            ctype, disposition = "application/octet-stream", "attachment"
        else:
            disposition = "attachment" if query.get("download") == "1" else "inline"
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng:
            m = RANGE_RE.match(rng.strip())
            if m and (m.group(1) or m.group(2)):
                a, b = m.group(1), m.group(2)
                if a:
                    start = int(a)
                    end = min(int(b), size - 1) if b else size - 1
                else:
                    n = int(b)
                    start, end = max(0, size - n), size - 1
                    if n == 0:
                        start = size
                if start >= size or end < start:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self._common_headers()
                    self.end_headers()
                    return
                status = 206
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(max(0, end - start + 1)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-store")  # no copies in the browser's cache
        self.send_header("Content-Disposition", "%s; filename*=UTF-8''%s" % (
            disposition, quote(file, safe="")))
        self.send_header("Content-Security-Policy", FILE_CSP)
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self._common_headers()
        self.end_headers()
        if not head and end >= start:
            self._copy(path, start, end - start + 1)

    def _copy(self, path: str, start: int, length: int) -> None:
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"  # on Windows SO_REUSEADDR lets two servers share a port

    def __init__(self, port: int, store: Optional[Store] = None, host: str = "127.0.0.1",
                 web_dir: str = WEB_DIR) -> None:
        self.store = store  # type: ignore[assignment]
        self.web_dir = web_dir
        self.wiped = False  # set by "Delete all data", which also stops the server
        self.quitting = False  # set by Quit (in the window, or `notetoself --stop`)
        super().__init__((host, port), Handler)
