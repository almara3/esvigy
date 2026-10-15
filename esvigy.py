#!/usr/bin/env python3
"""esvigy - a fast SVG viewer that uses Firefox as its rendering engine.

Runs a tiny local HTTP server over a directory and opens the UI in the browser.
Thumbnails are rendered once by the browser and cached on disk.
"""

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

STATIC_DIR = Path(__file__).resolve().parent / "static"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "esvigy" / "thumbs"
SVG_EXTS = (".svg", ".svgz")
HEAD_BYTES = 64 * 1024
MAX_THUMB_BYTES = 4 * 1024 * 1024
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}

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

    def resolve(self, rel):
        """Map a client-supplied relative path to a real path inside the root, or None."""
        root = self.server.root
        try:
            p = (root / rel.lstrip("/")).resolve()
        except (OSError, RuntimeError):
            return None
        return p if p == root or p.is_relative_to(root) else None

    def host_ok(self):
        # Guard against DNS rebinding: only answer requests addressed to us.
        host = self.headers.get("Host", "")
        return host in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

    # --- routing -----------------------------------------------------------

    def do_GET(self):
        if not self.host_ok():
            return self.fail(HTTPStatus.FORBIDDEN, "bad host")
        url = urlsplit(self.path)
        path, query = unquote(url.path), parse_qs(url.query, keep_blank_values=True)
        if path in ("/", "/index.html"):
            return self.serve_static("index.html")
        if path.startswith("/static/"):
            return self.serve_static(path[len("/static/"):])
        if path == "/api/list":
            return self.api_list(query.get("dir", [""])[0])
        if path.startswith("/file/"):
            return self.serve_svg(path[len("/file/"):], "fix" in query)
        if path.startswith("/thumb/"):
            return self.serve_thumb(path[len("/thumb/"):])
        self.fail(HTTPStatus.NOT_FOUND)

    do_HEAD = do_GET

    def do_PUT(self):
        if not self.host_ok():
            return self.fail(HTTPStatus.FORBIDDEN, "bad host")
        path = unquote(urlsplit(self.path).path)
        if not path.startswith("/thumb/"):
            return self.fail(HTTPStatus.NOT_FOUND)
        name = path[len("/thumb/"):]
        length = int(self.headers.get("Content-Length") or 0)
        if not re.fullmatch(r"[0-9a-f]{24}\.webp", name) or not 0 < length <= MAX_THUMB_BYTES:
            return self.fail(HTTPStatus.BAD_REQUEST)
        data = self.rfile.read(length)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_DIR / f".{name}.{threading.get_ident()}"
        tmp.write_bytes(data)
        tmp.replace(CACHE_DIR / name)
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # --- endpoints ---------------------------------------------------------

    def serve_static(self, rel):
        p = (STATIC_DIR / rel).resolve()
        if not p.is_relative_to(STATIC_DIR) or not p.is_file():
            return self.fail(HTTPStatus.NOT_FOUND)
        self.send_bytes(p.read_bytes(), STATIC_TYPES.get(p.suffix, "application/octet-stream"),
                        headers={"Cache-Control": "no-cache"})

    def api_list(self, rel):
        d = self.resolve(rel)
        if d is None or not d.is_dir():
            return self.fail(HTTPStatus.NOT_FOUND)
        dirs, files = [], []
        try:
            entries = list(os.scandir(d))
        except OSError as e:
            return self.fail(HTTPStatus.FORBIDDEN, str(e))
        for e in entries:
            if e.name.startswith("."):
                continue
            try:
                if e.is_dir():
                    dirs.append(e.name)
                elif e.is_file() and e.name.lower().endswith(SVG_EXTS):
                    p = Path(e.path)
                    st = e.stat()
                    w, h, intrinsic = svg_size(read_head(p))
                    key = thumb_key(p.resolve(), st)
                    files.append({"name": e.name, "size": st.st_size, "mtime": st.st_mtime,
                                  "w": round(w, 3), "h": round(h, 3), "intrinsic": intrinsic,
                                  "key": key, "thumb": (CACHE_DIR / f"{key}.webp").exists()})
            except OSError:
                continue
        rel_root = "" if d == self.server.root else str(d.relative_to(self.server.root))
        self.send_json({"dir": rel_root, "root": self.server.root.name or str(self.server.root),
                        "dirs": dirs, "files": files})

    def serve_svg(self, rel, fix):
        p = self.resolve(rel)
        if p is None or not p.is_file() or not p.name.lower().endswith(SVG_EXTS):
            return self.fail(HTTPStatus.NOT_FOUND)
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
                   # SVG can carry scripts; never let it run anything when opened directly.
                   "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:"}
        if gz:
            headers["Content-Encoding"] = "gzip"

        head, skip = b"", 0
        if fix and not gz:
            with open(p, "rb") as f:
                raw = f.read(HEAD_BYTES)
            w, h, intrinsic = svg_size(raw)
            if not intrinsic:
                head, skip = with_intrinsic_size(raw, w, h), len(raw)

        length = len(head) + st.st_size - skip
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/svg+xml")
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
        if not re.fullmatch(r"[0-9a-f]{24}\.webp", name):
            return self.fail(HTTPStatus.NOT_FOUND)
        p = CACHE_DIR / name
        if not p.is_file():
            return self.fail(HTTPStatus.NOT_FOUND)
        # Keys change whenever the source file changes, so thumbs are immutable.
        self.send_bytes(p.read_bytes(), "image/webp", headers={"Cache-Control": "max-age=31536000, immutable"})


def open_browser(url, browser):
    if browser == "none":
        return
    exe = shutil.which("firefox") if browser in ("auto", "firefox") else None
    if exe:
        subprocess.Popen([exe, "--new-window", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    else:
        webbrowser.open(url)


def main():
    ap = argparse.ArgumentParser(description="Fast SVG viewer (renders with Firefox).")
    ap.add_argument("path", nargs="?", default=".", help="directory (or SVG file) to browse")
    ap.add_argument("-p", "--port", type=int, default=0, help="port to listen on (default: random)")
    ap.add_argument("-b", "--browser", choices=("auto", "firefox", "default", "none"), default="auto",
                    help="browser to open (default: firefox if installed)")
    ap.add_argument("-v", "--verbose", action="store_true", help="log every request")
    args = ap.parse_args()

    target = Path(args.path).expanduser().resolve()
    if not target.exists():
        sys.exit(f"esvigy: {target}: no such file or directory")
    root = target if target.is_dir() else target.parent

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    server.root, server.verbose = root, args.verbose
    url = f"http://127.0.0.1:{server.server_port}/"
    if target.is_file():
        url += "#file=" + quote(target.name)

    print(f"esvigy: serving {root}\n        {url}\n        Ctrl+C to quit", flush=True)
    open_browser(url, args.browser)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
