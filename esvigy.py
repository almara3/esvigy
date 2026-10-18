#!/usr/bin/env python3
"""esvigy - a fast SVG viewer that uses Firefox (or else Chromium) as its rendering engine.

Runs a tiny local HTTP server over a directory and opens the UI in the browser.
Thumbnails are rendered once by the browser and cached on disk.
"""

import argparse
import gzip
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

STATIC_DIR = Path(__file__).resolve().parent / "static"
CACHE_DIR = Path(os.environ.get("ESVIGY_CACHE")
                 or Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "esvigy" / "thumbs")
SVG_EXTS = (".svg", ".svgz")
# Illustrator files since CS (PDF-compatible, the default) are PDFs inside.
PDF_EXTS = (".pdf", ".ai")
# Raster formats Firefox decodes itself.
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp", ".avif": "image/avif", ".bmp": "image/bmp", ".ico": "image/x-icon"}
# TIFF is decoded by the bundled UTIF.js (static/tiffworker.js).
TIFF_EXTS = (".tif", ".tiff")
DOC_EXTS = SVG_EXTS + PDF_EXTS + tuple(IMAGE_TYPES) + TIFF_EXTS
# PDFs can't be measured without a PDF parser. Until the browser has rendered one (and
# stored its size in the cache), assume an A4 page.
A4_PX = (793.701, 1122.52)
HEAD_BYTES = 64 * 1024
MAX_THUMB_BYTES = 4 * 1024 * 1024
MAX_META_BYTES = 1024
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
                ".mjs": "text/javascript; charset=utf-8", ".wasm": "application/wasm"}
THUMB_NAME_RE = re.compile(r"[0-9a-f]{24}\.webp")
META_NAME_RE = re.compile(r"[0-9a-f]{24}\.json")
# SVG can carry scripts; never let it run anything when opened directly.
SVG_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; sandbox"
# Raster images can't run anything; this is for PDFs and defence in depth.
# PDFs are drawn by our bundled pdf.js (fetched as data, so this doesn't apply there). Opened
# directly via "raw", the browser's own viewer would show them, and it ignores this CSP. Firefox's
# app profile switches PDF scripting off (see USER_JS); every other browser gets them as a download
# (server.pdf_inline), since there is no such switch elsewhere.
PDF_CSP = "default-src 'none'"

SVG_TAG_RE = re.compile(rb"<svg\b[^>]*>", re.S)
ATTR_RE = re.compile(rb"""\s([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
LENGTH_RE = re.compile(r"^\s*([0-9.eE+-]+)\s*(px|pt|pc|mm|cm|in|em|ex)?\s*$")
UNIT_PX = {None: 1, "px": 1, "pt": 4 / 3, "pc": 16, "mm": 96 / 25.4, "cm": 96 / 2.54,
           "in": 96, "em": 16, "ex": 8}


def parse_length(value):
    m = LENGTH_RE.match(value or "")
    if not m:
        return None
    try:
        v = float(m.group(1)) * UNIT_PX[m.group(2)]
    except ValueError:
        return None
    return v if v > 0 else None


def read_head(path):
    opener = gzip.open if path.suffix.lower() == ".svgz" else open
    try:
        with opener(path, "rb") as f:
            return f.read(HEAD_BYTES)
    except (OSError, EOFError):
        return b""


def svg_root_attrs(head):
    m = SVG_TAG_RE.search(head)
    if not m:
        return None, {}
    attrs = {}
    for a in ATTR_RE.finditer(m.group(0)):
        attrs[a.group(1).decode("latin-1")] = (a.group(2) or a.group(3) or b"").decode("latin-1")
    return m, attrs


def svg_size(head):
    """Return (width, height, has_intrinsic_size) in CSS px, from the root <svg> tag."""
    _, attrs = svg_root_attrs(head)
    w, h = parse_length(attrs.get("width")), parse_length(attrs.get("height"))
    vb = None
    try:
        parts = [float(x) for x in re.split(r"[\s,]+", attrs.get("viewBox", "").strip())]
        if len(parts) == 4 and parts[2] > 0 and parts[3] > 0:
            vb = parts[2], parts[3]
    except ValueError:
        pass
    if w and h:
        return w, h, True
    if vb:
        if w:
            return w, w * vb[1] / vb[0], False
        if h:
            return h * vb[0] / vb[1], h, False
        return vb[0], vb[1], False
    return w or 300, h or 150, False


def with_intrinsic_size(head, w, h):
    """Rewrite the root <svg> tag so it carries explicit width/height.

    Firefox refuses to draw SVGs without intrinsic size onto a canvas, which
    is how thumbnails are made.
    """
    m, _ = svg_root_attrs(head)
    if not m:
        return head
    tag = re.sub(rb"""\s(width|height)\s*=\s*("[^"]*"|'[^']*')""", b"", m.group(0))
    tag = tag[:4] + b' width="%.3f" height="%.3f"' % (w, h) + tag[4:]
    return head[:m.start()] + tag + head[m.end():]


def tiff_tags(read):
    """Numeric tags of a TIFF's first image directory, as {tag: first value}, or None.

    `read(offset, n)` returns bytes of the TIFF data. TIFFs often keep the directory at the end
    of the file, so it is read from wherever it is; EXIF blocks inside JPEGs are TIFFs too.
    """
    head = read(0, 8)
    e = {b"II*\0": "<", b"MM\0*": ">"}.get(head[:4])
    if not e:
        return None
    try:
        (off,) = struct.unpack(e + "I", head[4:8])
        (n,) = struct.unpack(e + "H", read(off, 2))
        entries = read(off + 2, 12 * min(n, 1024))
        tags = {}
        for i in range(0, len(entries) - 11, 12):
            tag, typ = struct.unpack(e + "HH", entries[i:i + 4])
            if typ == 3:  # SHORT
                tags[tag] = struct.unpack(e + "H", entries[i + 8:i + 10])[0]
            elif typ == 4:  # LONG
                tags[tag] = struct.unpack(e + "I", entries[i + 8:i + 12])[0]
        return tags
    except struct.error:
        return None


def jpeg_size(head):
    """(w, h) of a JPEG as displayed, i.e. after its EXIF orientation, or None."""
    i, orient, size = 2, 1, None
    try:
        while i + 4 <= len(head) and size is None:
            if head[i] != 0xFF:
                return None
            marker = head[i + 1]
            if marker == 0xFF:  # fill byte
                i += 1
                continue
            (n,) = struct.unpack(">H", head[i + 2:i + 4])
            seg = head[i + 4:i + 2 + n]
            if marker == 0xE1 and seg[:6] == b"Exif\0\0":
                tags = tiff_tags(lambda off, k, t=seg[6:]: t[off:off + k]) or {}
                orient = tags.get(0x0112, 1)
            elif 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):  # start of frame
                h, w = struct.unpack(">HH", seg[1:5])
                size = w, h
            i += 2 + n
    except struct.error:
        return None
    if size and 5 <= orient <= 8:  # rotated by 90°
        size = size[::-1]
    return size


def image_size(path):
    """(w, h) in px of a raster image, read from its header; None if not found.

    AVIF rotation and anything this misses is corrected once the browser has decoded the
    image (it stores the real size in the cache, which the listing prefers).
    """
    try:
        with open(path, "rb") as f:
            head = f.read(HEAD_BYTES)
            if head[:4] in (b"II*\0", b"MM\0*"):
                def read(off, n):
                    f.seek(off)
                    return f.read(n)
                tags = tiff_tags(read) or {}
                size = tags.get(256), tags.get(257)
                return size if all(size) else None
    except OSError:
        return None
    try:
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", head[16:24])
        if head[:4] == b"GIF8":
            return struct.unpack("<HH", head[6:10])
        if head[:2] == b"\xff\xd8":
            return jpeg_size(head)
        if head[:2] == b"BM":
            if struct.unpack("<I", head[14:18])[0] == 12:  # OS/2 header
                return struct.unpack("<HH", head[18:22])
            w, h = struct.unpack("<ii", head[18:26])
            return w, abs(h)  # negative height: stored top-down
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            chunk = head[12:16]
            if chunk == b"VP8 ":
                w, h = struct.unpack("<HH", head[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if chunk == b"VP8L":
                (b,) = struct.unpack("<I", head[21:25])
                return (b & 0x3FFF) + 1, (b >> 14 & 0x3FFF) + 1
            if chunk == b"VP8X":
                return (int.from_bytes(head[24:27], "little") + 1, int.from_bytes(head[27:30], "little") + 1)
            return None
        if head[:4] == b"\0\0\1\0":  # ICO: the biggest of its images; 0 means 256
            (n,) = struct.unpack("<H", head[4:6])
            sizes = [(head[6 + 16 * k] or 256, head[7 + 16 * k] or 256) for k in range(min(n, 64))]
            return max(sizes, key=lambda s: s[0] * s[1]) if sizes else None
        if head[4:8] == b"ftyp":  # AVIF: the image spatial extent ("ispe") property
            k = head.find(b"ispe")
            return struct.unpack(">II", head[k + 8:k + 16]) if k > 0 else None
    except (struct.error, IndexError):
        return None
    return None


def parse_meta(data):
    """Validate a document's metadata as sent by the browser; return a clean dict or None."""
    try:
        m = json.loads(data)
        w, h, pages = float(m["w"]), float(m["h"]), int(m["pages"])
    except (ValueError, TypeError, KeyError):
        return None
    if not (0 < w < 1e6 and 0 < h < 1e6 and 0 < pages < 1e6) or m["pages"] != pages:
        return None
    return {"w": round(w, 3), "h": round(h, 3), "pages": pages}


def read_meta(key):
    try:
        return parse_meta((CACHE_DIR / f"{key}.json").read_bytes()[:MAX_META_BYTES])
    except OSError:
        return None


def file_kind(name):
    """How the UI draws a file: "svg", "pdf", "image" (natively) or "tiff" (with UTIF.js)."""
    ext = os.path.splitext(name)[1].lower()
    if ext in SVG_EXTS:
        return "svg"
    if ext in PDF_EXTS:
        return "pdf"
    return "tiff" if ext in TIFF_EXTS else "image"


def thumb_key(path, st):
    raw = f"{path}\0{st.st_mtime_ns}\0{st.st_size}".encode()
    return hashlib.sha1(raw).hexdigest()[:24]


class Handler(BaseHTTPRequestHandler):
    server_version = "esvigy"
    protocol_version = "HTTP/1.1"

    # --- helpers -----------------------------------------------------------

    def log_message(self, fmt, *args):
        if self.server.verbose:
            super().log_message(fmt, *args)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def send_bytes(self, body, ctype, status=HTTPStatus.OK, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, obj):
        self.send_bytes(json.dumps(obj).encode(), "application/json", headers={"Cache-Control": "no-store"})

    def fail(self, status, msg=""):
        self.send_bytes(msg.encode(), "text/plain; charset=utf-8", status)

    @staticmethod
    def resolve(root, rel):
        """Map a client-supplied relative path to a real path inside `root`, or None."""
        # On Windows, "\\server\share" or "C:" would make resolve() touch other drives or the
        # network (leaking the login hash over SMB) before the check below; ":" also names NTFS streams.
        if sys.platform == "win32" and ("\\" in rel or ":" in rel):
            return None
        try:
            p = (root / rel.lstrip("/")).resolve()
        except (OSError, RuntimeError, ValueError):  # ValueError: NUL byte in the path
            return None
        return p if p == root or p.is_relative_to(root) else None

    def host_ok(self):
        # Guard against DNS rebinding: only answer requests addressed to us.
        host = self.headers.get("Host", "")
        return host in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

    def route_path(self):
        """Check host and token; return the decoded path below /<token>, or None after replying.

        Every URL starts with a random secret, so other local users and web pages that
        guess the port can't list folders, read files, probe for them or write thumbnails.
        """
        if not self.host_ok():
            self.fail(HTTPStatus.FORBIDDEN, "bad host")
            return None
        # Split before decoding, so an encoded "/" can't move the token boundary.
        token, sep, rest = urlsplit(self.path).path[1:].partition("/")
        if not hmac.compare_digest(token.encode(), self.server.token.encode()):
            self.fail(HTTPStatus.FORBIDDEN, "open the URL printed by esvigy")
            return None
        if not sep:  # /<token> -> /<token>/, so relative URLs resolve
            self.send_response(HTTPStatus.MOVED_PERMANENTLY)
            self.send_header("Location", f"/{token}/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        return "/" + unquote(rest)

    # --- routing -----------------------------------------------------------

    def do_GET(self):
        path = self.route_path()
        if path is None:
            return
        query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        if path in ("/", "/index.html"):
            if self.server.launch_page:  # the browser has arrived: the forwarding page is done
                self.server.launch_page.unlink(missing_ok=True)
                self.server.launch_page = None
            return self.serve_static("index.html")
        if path.startswith("/static/"):
            return self.serve_static(path[len("/static/"):])
        if path == "/api/list":
            return self.api_list(query.get("dir", [""])[0])
        if path.startswith("/file/"):
            return self.serve_file(path[len("/file/"):], "fix" in query)
        if path.startswith("/thumb/"):
            return self.serve_thumb(path[len("/thumb/"):])
        self.fail(HTTPStatus.NOT_FOUND)

    do_HEAD = do_GET

    def write_path(self):
        """route_path() for requests that change state; they also need the X-Esvigy header."""
        path = self.route_path()
        if path is None:
            return None
        # A custom header can't be sent cross-site without a CORS preflight, which we never grant.
        if self.headers.get("X-Esvigy") != "1":
            self.fail(HTTPStatus.FORBIDDEN, "missing header")
            return None
        return path

    def do_POST(self):
        path = self.write_path()
        if path is None:
            return
        if path != "/api/open":
            return self.fail(HTTPStatus.NOT_FOUND)
        self.api_open()

    def do_PUT(self):
        path = self.write_path()
        if path is None:
            return
        if not path.startswith("/thumb/"):
            return self.fail(HTTPStatus.NOT_FOUND)
        name = path[len("/thumb/"):]
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        is_meta = bool(META_NAME_RE.fullmatch(name))
        limit = MAX_META_BYTES if is_meta else MAX_THUMB_BYTES
        if not (is_meta or THUMB_NAME_RE.fullmatch(name)) or not 0 < length <= limit:
            self.close_connection = True  # the body (if any) is left unread
            return self.fail(HTTPStatus.BAD_REQUEST)
        data = self.rfile.read(length)
        if len(data) != length:
            return self.fail(HTTPStatus.BAD_REQUEST)
        if is_meta:
            meta = parse_meta(data)
            if meta is None:
                return self.fail(HTTPStatus.BAD_REQUEST, "bad metadata")
            data = json.dumps(meta).encode()  # store only what we validated
        elif data[:4] != b"RIFF" or data[8:12] != b"WEBP":
            return self.fail(HTTPStatus.BAD_REQUEST, "not a webp image")
        CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = CACHE_DIR / f".{name}.{threading.get_ident()}"
        tmp.write_bytes(data)
        try:
            tmp.replace(CACHE_DIR / name)
        except PermissionError:
            # Windows can't replace a file that is open (being served, or a racing upload of the
            # same key). Keys are content-addressed, so the copy already there is just as good.
            tmp.unlink(missing_ok=True)
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # --- endpoints ---------------------------------------------------------

    def serve_static(self, rel):
        p = self.resolve(STATIC_DIR, rel)
        if p is None or not p.is_file():
            return self.fail(HTTPStatus.NOT_FOUND)
        headers = {"Cache-Control": "no-cache"}
        if p.suffix == ".html":
            port = self.server.server_port
            loopback = f"http://127.0.0.1:{port} http://localhost:{port}"
            # The UI may not be framed by anyone; the renderer only by the UI on the other loopback name.
            ancestors = loopback if p.name == "render.html" else "'none'"
            headers["Content-Security-Policy"] = (
                f"default-src 'self'; img-src 'self' blob:; frame-src {loopback}; "
                f"frame-ancestors {ancestors}; base-uri 'none'; form-action 'none'; object-src 'none'")
        self.send_bytes(p.read_bytes(), STATIC_TYPES.get(p.suffix, "application/octet-stream"), headers=headers)

    def api_list(self, rel):
        root = self.server.root  # once: /api/open may swap it while we work
        d = self.resolve(root, rel)
        if d is None or not d.is_dir():
            return self.fail(HTTPStatus.NOT_FOUND)
        dirs, files = [], []
        try:
            entries = list(os.scandir(d))
        except OSError:
            return self.fail(HTTPStatus.FORBIDDEN, "cannot read folder")
        for e in entries:
            if e.name.startswith("."):
                continue
            try:
                # Symlinks leading out of the root would 404 when opened; don't list (or read) them.
                real = Path(e.path).resolve()
                if not real.is_relative_to(root):
                    continue
                if e.is_dir():
                    dirs.append(e.name)
                elif e.is_file() and e.name.lower().endswith(DOC_EXTS):
                    st = e.stat()
                    key = thumb_key(real, st)
                    f = {"name": e.name, "size": st.st_size, "mtime": st.st_mtime,
                         "key": key, "thumb": (CACHE_DIR / f"{key}.webp").exists()}
                    kind = file_kind(e.name)
                    if kind == "pdf":
                        meta = read_meta(key)
                        w, h = (meta["w"], meta["h"]) if meta else A4_PX
                        f.update(kind="pdf", pages=meta and meta["pages"], intrinsic=True)
                    elif kind != "svg":
                        # The browser's measurement (rotation applied, TIFF page count) beats the header.
                        meta = read_meta(key)
                        size = (meta["w"], meta["h"]) if meta else image_size(real)
                        if size and size[0] > 0 and size[1] > 0:
                            w, h = size
                            pages = meta["pages"] if meta else 1
                        else:  # unknown until the browser has decoded it
                            (w, h), pages = A4_PX, None
                        f.update(kind=kind, pages=pages, intrinsic=True)
                    else:
                        w, h, intrinsic = svg_size(read_head(real))
                        f.update(kind="svg", intrinsic=intrinsic)
                    f.update(w=round(w, 3), h=round(h, 3))
                    files.append(f)
            except (OSError, RuntimeError):
                continue
        rel_root = "" if d == root else d.relative_to(root).as_posix()
        self.send_json({"dir": rel_root, "root": root.name or str(root), "dirs": dirs, "files": files})

    def api_open(self):
        """Pick a folder with the desktop's native dialog and make it the new root."""
        cmd = folder_dialog_cmd(self.server.root)
        if cmd is None:
            return self.fail(HTTPStatus.NOT_IMPLEMENTED, "install zenity or kdialog to use Open")
        with self.server.open_lock:
            try:
                # The start folder goes in through the environment, never into a script's source.
                r = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace",
                                   env={**os.environ, "ESVIGY_START": str(self.server.root)})
            except OSError:
                return self.fail(HTTPStatus.INTERNAL_SERVER_ERROR, "cannot start the folder dialog")
            chosen = Path(r.stdout.strip()).expanduser() if r.returncode == 0 and r.stdout.strip() else None
            if chosen is None or not chosen.is_dir():
                return self.send_bytes(b"", "text/plain", HTTPStatus.NO_CONTENT)  # cancelled
            self.server.root = chosen.resolve()
        self.send_json({"root": self.server.root.name or str(self.server.root)})

    def serve_file(self, rel, fix):
        p = self.resolve(self.server.root, rel)
        if p is None or not p.is_file() or not p.name.lower().endswith(DOC_EXTS):
            return self.fail(HTTPStatus.NOT_FOUND)
        kind = file_kind(p.name)
        fix = fix and kind == "svg"
        st = p.stat()
        etag = f'"{thumb_key(p, st)}{"f" if fix else ""}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        gz = p.suffix.lower() == ".svgz"
        headers = {"ETag": etag, "Cache-Control": "no-cache",
                   "Content-Security-Policy": SVG_CSP if kind == "svg" else PDF_CSP}
        if gz:
            headers["Content-Encoding"] = "gzip"
        if kind == "pdf" and not self.server.pdf_inline:
            # Only navigations obey this; pdf.js fetches the file and doesn't care.
            headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(p.name)

        head, skip = b"", 0
        if fix and not gz:
            with open(p, "rb") as f:
                raw = f.read(HEAD_BYTES)
            w, h, intrinsic = svg_size(raw)
            if not intrinsic:
                head, skip = with_intrinsic_size(raw, w, h), len(raw)

        length = len(head) + st.st_size - skip
        self.send_response(HTTPStatus.OK)
        ctype = {"svg": "image/svg+xml", "pdf": "application/pdf", "tiff": "image/tiff"}.get(kind)
        self.send_header("Content-Type", ctype or IMAGE_TYPES[p.suffix.lower()])
        self.send_header("Content-Length", str(length))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            self.wfile.write(head)
            with open(p, "rb") as f:
                f.seek(skip)
                shutil.copyfileobj(f, self.wfile, 1024 * 1024)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def serve_thumb(self, name):
        if not THUMB_NAME_RE.fullmatch(name):  # metadata is only delivered through the listing
            return self.fail(HTTPStatus.NOT_FOUND)
        p = CACHE_DIR / name
        if not p.is_file():
            return self.fail(HTTPStatus.NOT_FOUND)
        # Keys change whenever the source file changes, so thumbs are immutable.
        self.send_bytes(p.read_bytes(), "image/webp", headers={"Cache-Control": "max-age=31536000, immutable"})


# Native folder pickers. Each prints the chosen path and exits non-zero on cancel.
MAC_DIALOG = ('POSIX path of (choose folder with prompt "Open folder" '
              'default location (POSIX file (system attribute "ESVIGY_START")))')
WIN_DIALOG = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
              "Add-Type -AssemblyName System.Windows.Forms; "
              "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
              "$d.SelectedPath = $env:ESVIGY_START; "
              "if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath } else { exit 1 }")


def find_program(name):
    """Full path of program `name` from the absolute PATH entries, or None.

    Not shutil.which(): on Windows it (and subprocess, given a bare name) looks in the current
    directory first and accepts .bat/.cmd, so a planted firefox.bat in the folder esvigy was
    started from would run. Here relative PATH entries are skipped and only .exe counts on Windows.
    """
    ext = ".exe" if sys.platform == "win32" else ""
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or not os.path.isabs(d):
            continue
        p = Path(d) / (name + ext)
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def folder_dialog_cmd(start):
    """Command line (with the program's full path) for the desktop's folder picker, or None."""
    if sys.platform == "win32":
        ps = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        return [str(ps), "-NoProfile", "-NonInteractive", "-STA", "-Command", WIN_DIALOG] if ps.is_file() else None
    if sys.platform == "darwin":
        return ["/usr/bin/osascript", "-e", MAC_DIALOG] if Path("/usr/bin/osascript").is_file() else None
    if exe := find_program("zenity"):
        return [exe, "--file-selection", "--directory", "--title=Open folder", f"--filename={start}/"]
    if exe := find_program("kdialog"):
        return [exe, "--getexistingdirectory", str(start)]
    return None


def find_firefox():
    """Firefox's full path. On macOS and Windows it is usually not on PATH, so look where it gets installed first."""
    if sys.platform == "darwin":
        candidates = [Path(base) / "Firefox.app" / "Contents" / "MacOS" / "firefox"
                      for base in ("/Applications", Path.home() / "Applications")]
    elif sys.platform == "win32":
        candidates = [Path(os.environ[var]) / "Mozilla Firefox" / "firefox.exe"
                      for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")
                      if os.path.isabs(os.environ.get(var, ""))]
    else:
        candidates = []
    return next((str(p) for p in candidates if p.is_file()), None) or find_program("firefox")


# Chromium-based browsers, in order of preference. Edge comes with Windows, so there is always one.
CHROMIUM_PROGRAMS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome",
                     "microsoft-edge", "microsoft-edge-stable", "msedge", "brave-browser", "brave",
                     "vivaldi-stable", "vivaldi")
CHROMIUM_MAC_APPS = ("Google Chrome", "Chromium", "Microsoft Edge", "Brave Browser", "Vivaldi")
CHROMIUM_WIN_EXES = (("Google", "Chrome", "Application", "chrome.exe"),
                     ("Chromium", "Application", "chrome.exe"),
                     ("Microsoft", "Edge", "Application", "msedge.exe"),
                     ("BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
                     ("Vivaldi", "Application", "vivaldi.exe"))


def find_chromium():
    """Full path of a Chromium-based browser (Chrome, Chromium, Edge, Brave, Vivaldi), looked up like find_firefox()."""
    if sys.platform == "darwin":
        candidates = [Path(base) / f"{app}.app" / "Contents" / "MacOS" / app
                      for app in CHROMIUM_MAC_APPS for base in ("/Applications", Path.home() / "Applications")]
    elif sys.platform == "win32":
        bases = [os.environ[var] for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")
                 if os.path.isabs(os.environ.get(var, ""))]
        candidates = [Path(base, *rel) for rel in CHROMIUM_WIN_EXES for base in bases]
    else:
        candidates = []
    return (next((str(p) for p in candidates if p.is_file()), None)
            or next(filter(None, map(find_program, CHROMIUM_PROGRAMS)), None))


DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "esvigy"
PROFILE_DIR = DATA_DIR / "firefox-profile"
USER_JS = """\
user_pref("toolkit.legacyUserProfileCustomizations.stylesheets", true);
user_pref("browser.tabs.inTitlebar", 0);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.startup.page", 0);
user_pref("browser.aboutwelcome.enabled", false);
user_pref("browser.tabs.warnOnClose", false);
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("datareporting.healthreport.uploadEnabled", false);
user_pref("app.update.auto", false);
// Firefox's own PDF viewer ("raw" link) would run JavaScript embedded in PDFs; the CSP doesn't stop it.
user_pref("pdfjs.enableScripting", false);
"""
USER_CHROME_CSS = """\
/* esvigy: hide tabs, address bar and bookmarks; keep only the page and the window frame. */
#TabsToolbar, #nav-bar, #PersonalToolbar, #toolbar-menubar { visibility: collapse !important; }
"""


def prepare_profile():
    # History in the profile holds the secret URLs; keep it private.
    PROFILE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    (PROFILE_DIR / "chrome").mkdir(exist_ok=True)
    (PROFILE_DIR / "user.js").write_text(USER_JS)
    (PROFILE_DIR / "chrome" / "userChrome.css").write_text(USER_CHROME_CSS)


def chromium_profile():
    """A new, private (0700) data folder for one Chromium app window, removed when esvigy quits.

    A fresh one per run, not one kept like Firefox's: started with a folder that a running
    Chromium already uses, Chromium hands the window over to it and exits at once, which would
    look to us like the window was closed. Nothing in it is worth keeping (the URL changes every run).
    """
    return tempfile.mkdtemp(prefix="esvigy-chromium-")


def remove_profile(profile, proc=None, timeout=10):
    """Delete a Chromium data folder from chromium_profile() once the browser `proc` is gone.

    Chromium's helper processes (network service, zygote) outlive the main process for a moment
    and keep writing into the folder (its cache holds copies of viewed files), so a single
    rmtree() can leave it behind. Wait for the browser's whole process group (open_browser()
    starts it in its own), then delete until the folder stays gone.
    """
    deadline = time.monotonic() + timeout
    if proc is not None:
        try:
            proc.wait(max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            proc.kill()
        # Only a group of its own (never ours: pid 0 or our group would signal esvigy itself).
        if hasattr(os, "killpg") and proc.pid > 0 and proc.pid != os.getpgrp():
            try:
                while time.monotonic() < deadline:
                    os.killpg(proc.pid, 0)  # raises once no process of the group is left
                    time.sleep(0.05)
                os.killpg(proc.pid, 9)  # stragglers: SIGKILL (signal.SIGKILL isn't on Windows)
            except (ProcessLookupError, PermissionError):
                pass
    for _ in range(20):  # Windows may hold files a moment longer
        shutil.rmtree(profile, ignore_errors=True)
        if not os.path.exists(profile):
            return
        time.sleep(0.1)


def choose_browser(browser):
    """(mode, exe) for -b BROWSER. mode is "firefox-app", "chromium-app", "firefox", "default" or "none"."""
    if browser in ("auto", "app"):
        if exe := find_firefox():
            return "firefox-app", exe
        if exe := find_chromium():
            return "chromium-app", exe
        return "default", None
    if browser in ("chromium", "firefox"):
        exe = find_chromium() if browser == "chromium" else find_firefox()
        if exe:
            return ("chromium-app" if browser == "chromium" else "firefox"), exe
        print(f"esvigy: no {'Chromium-based browser' if browser == 'chromium' else 'Firefox'} found, "
              "opening the default browser instead", file=sys.stderr)
        return "default", None
    return browser, None


def launch_page(url):
    """Write a private page that forwards the browser to `url`; return its path.

    The secret URL must never go on a browser's command line: other users on the machine can
    read command lines (ps, /proc/<pid>/cmdline), and an app window keeps its own for as long as
    it is open. The browser gets this page's path instead, which gives nothing away; the page
    itself is readable only by us. The server deletes it as soon as the browser has arrived
    (Handler.do_GET), main() when esvigy quits at the latest.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.chmod(0o700)
    page = DATA_DIR / f"open-{secrets.token_hex(8)}.html"
    # O_EXCL | O_NOFOLLOW: never write through a file or symlink someone put there.
    fd = os.open(page, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    u = html.escape(url, quote=True)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f'<!doctype html>\n<meta charset="utf-8">\n<title>esvigy</title>\n'
                f'<meta http-equiv="refresh" content="0; url={u}">\n<p><a href="{u}">Open esvigy</a></p>\n')
    return page


def open_browser(url, mode, exe):
    """Launch the UI. Returns (process, data folder to remove afterwards) for an app window, else (None, None)."""
    if mode == "firefox-app":
        prepare_profile()
        args = [exe, "--no-remote", "--profile", str(PROFILE_DIR)]
        if sys.platform == "win32":
            args.append("--wait-for-browser")  # firefox.exe is a launcher that exits right away otherwise
        elif sys.platform != "darwin":
            args += ["--class", "esvigy", "--name", "esvigy"]  # X11/Wayland window class
        return subprocess.Popen(args + ["--new-window", url], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL), None
    if mode == "chromium-app":
        profile = chromium_profile()
        # --app: a window without tabs, address bar or bookmarks (what userChrome.css does for Firefox).
        args = [exe, f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check"]
        if sys.platform not in ("win32", "darwin"):
            args.append("--class=esvigy")  # X11 window class
        # Its own process group, so remove_profile() can wait for all of its processes.
        return subprocess.Popen(args + [f"--app={url}"], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True), profile
    if mode == "firefox":
        subprocess.Popen([exe, "--new-window", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    elif mode == "default":
        webbrowser.open(url)
    return None, None


def make_server(root, port=0, verbose=False):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.root, server.verbose = Path(root).resolve(), verbose
    server.open_lock = threading.Lock()
    server.token = secrets.token_urlsafe(24)
    server.pdf_inline = False  # True only for a browser that won't run PDF scripts (Firefox app profile)
    server.url = f"http://127.0.0.1:{server.server_port}/{server.token}/"
    server.launch_page = None  # set by main(): the page that forwards the browser to server.url
    return server


def main():
    ap = argparse.ArgumentParser(description="Fast SVG viewer (renders with Firefox, or else Chromium).")
    ap.add_argument("path", nargs="?", default=".", help="directory (or SVG file) to browse")
    ap.add_argument("-p", "--port", type=int, default=0, help="port to listen on (default: random)")
    ap.add_argument("-b", "--browser", choices=("auto", "app", "chromium", "firefox", "default", "none"),
                    default="auto",
                    help="app (= auto): own window without tabs/toolbars that quits esvigy when closed, in Firefox "
                         "if installed, else in Chrome/Chromium/Edge/Brave/Vivaldi; chromium: that window in Chromium even "
                         "if Firefox is there; firefox: normal new Firefox window; default: system browser; "
                         "none: only print the URL")
    ap.add_argument("-v", "--verbose", action="store_true", help="log every request")
    args = ap.parse_args()

    target = Path(args.path).expanduser().resolve()
    if not target.exists():
        sys.exit(f"esvigy: {target}: no such file or directory")
    root = target if target.is_dir() else target.parent

    server = make_server(root, args.port, args.verbose)
    url = server.url
    if target.is_file():
        url += "#file=" + quote(target.name)

    print(f"esvigy: serving {root}\n        {url}\n        close the window or Ctrl+C to quit", flush=True)
    mode, exe = choose_browser(args.browser)
    server.pdf_inline = mode == "firefox-app"
    # The browser is pointed at a private page that forwards it, never at the secret URL itself.
    page = server.launch_page = launch_page(url) if mode != "none" else None
    proc, profile = open_browser(page.as_uri(), mode, exe) if page else (None, None)
    if proc:
        # Quit together with the window. With its own profile (Firefox: --no-remote), the browser
        # process lives exactly as long as it.
        threading.Thread(target=lambda: (proc.wait(), server.shutdown()), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
        if proc and proc.poll() is None:
            proc.terminate()
    finally:
        if profile:
            remove_profile(profile, proc)
        if page:
            page.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
