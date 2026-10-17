#!/usr/bin/env python3
"""Time how long it takes to render a big SVG: librsvg (what eog/xviewer use), resvg, and the
engines esvigy could run on: Firefox, Chromium and WebKitGTK.

    benchmark/gen.py heavy /tmp/heavy
    .venv/bin/python benchmark/bench.py /tmp/heavy [more folders or .svg files ...]

librsvg is driven through GdkPixbuf in a subprocess of the system python3 (needs PyGObject), exactly
the call image viewers and file-manager thumbnailers make. resvg is its command-line tool (PATH, or
RESVG=/path/to/resvg), timed including process start and PNG writing. The browsers load the file as
an <img>, draw it into a canvas and read a pixel back, which is what esvigy's thumbnail renderer does;
the read-back makes sure the pixels really exist (Chrome rasterizes lazily). Firefox and Chromium
are headless via selenium (Chrome for Testing is downloaded on first run if Chrome is missing),
WebKitGTK is the system library (what pywebview/Tauri use on Linux) via PyGObject, under xvfb-run
if available. Everything renders to 1600x1200 pixels on the CPU. Each file is run RUNS times; the
median is reported. Engines that aren't available are skipped.
"""

import functools
import http.server
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

RUNS = 3
TIMEOUT = 300
W, H = 1600, 1200

PIXBUF = f"""
import sys, time, gi
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import GdkPixbuf
t = time.perf_counter()
GdkPixbuf.Pixbuf.new_from_file_at_size(sys.argv[1], {W}, {H})
print(time.perf_counter() - t)
"""

# Body of an async JS function with arguments url, w, h; resolves to seconds or -1 on error.
RENDER_JS = """
    const img = new Image();
    const t = performance.now();
    img.src = url + "?" + Math.random();  // new URL each run, so no engine can reuse a cached render
    try { await new Promise((ok, fail) => { img.onload = ok; img.onerror = fail; }); }
    catch (e) { return -1; }
    const canvas = document.createElement("canvas");
    canvas.width = w; canvas.height = h;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(img, 0, 0, w, h);
    ctx.getImageData(0, 0, 1, 1);
    return (performance.now() - t) / 1000;
"""

# Long-lived WebKitGTK renderer: reads "base url" lines on stdin, prints seconds per line.
WEBKIT = f"""
import sys, gi
gi.require_version('Gtk', '3.0')
gi.require_version('WebKit2', '4.1')
from gi.repository import Gtk, GLib, WebKit2
print(f"{{WebKit2.get_major_version()}}.{{WebKit2.get_minor_version()}}.{{WebKit2.get_micro_version()}}", flush=True)
win = Gtk.OffscreenWindow()
view = WebKit2.WebView()
view.set_size_request({W}, {H})
win.add(view)
win.show_all()

def wait_load(base):
    loop = GLib.MainLoop()
    def changed(v, ev):
        if ev == WebKit2.LoadEvent.FINISHED:
            v.disconnect(hid); loop.quit()
    hid = view.connect('load-changed', changed)
    view.load_uri(base + '/')
    loop.run()

def render(url):
    loop, out = GLib.MainLoop(), []
    args = GLib.Variant('a{{sv}}', {{'url': GLib.Variant('s', url), 'w': GLib.Variant('i', {W}),
                                    'h': GLib.Variant('i', {H})}})
    def done(v, res):
        try: out.append(v.call_async_javascript_function_finish(res).to_double())
        except Exception as e: out.append(-1.0); print(e, file=sys.stderr)
        loop.quit()
    view.call_async_javascript_function({RENDER_JS!r}, -1, args, None, None, None, done)
    loop.run()
    return out[0]

for line in sys.stdin:
    base, url = line.split()
    wait_load(base)
    print(render(url), flush=True)
"""


def serve(folder, names):
    """Local HTTP server for the browsers (a page from file:// or about:blank may not load other files).

    It serves a blank page at / and the files `names` in `folder`, nothing else: no directory
    listing, no index.html, no other files, so benchmarking ~/work/fig.svg doesn't expose ~/work.
    """
    names = set(names)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            # Only requests addressed to us (no DNS rebinding), and only the listed files.
            if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
                return self.send_error(403)
            path = unquote(urlsplit(self.path).path)
            if path == "/":
                body = b"<!doctype html><title>bench</title>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
            elif path[1:] in names:
                super().do_GET()
            else:
                self.send_error(404)

        do_HEAD = do_GET

    handler = functools.partial(Handler, directory=str(folder))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Librsvg:
    name = "librsvg (eog, xviewer)"

    def __init__(self):
        if not Path("/usr/bin/python3").is_file():
            raise RuntimeError("no system python3")
        self.version = subprocess.run(
            ["/usr/bin/python3", "-c", "import gi; gi.require_version('Rsvg', '2.0'); from gi.repository import Rsvg;"
             "print(f'{Rsvg.MAJOR_VERSION}.{Rsvg.MINOR_VERSION}.{Rsvg.MICRO_VERSION}')"],
            capture_output=True, text=True, check=True).stdout.strip()

    def render(self, base, path):
        try:
            out = subprocess.run(["/usr/bin/python3", "-c", PIXBUF, str(path)], capture_output=True, text=True,
                                 timeout=TIMEOUT, check=True).stdout
            return float(out)
        except subprocess.TimeoutExpired:
            return None

    def close(self):
        pass


class Resvg:
    name = "resvg"

    def __init__(self):
        self.exe = os.environ.get("RESVG") or shutil.which("resvg")
        if not self.exe:
            raise RuntimeError("resvg not found (PATH or RESVG=)")
        self.version = subprocess.run([self.exe, "--version"], capture_output=True, text=True).stdout.strip()
        self.tmp = tempfile.TemporaryDirectory()

    def render(self, base, path):
        t = time.perf_counter()
        try:
            subprocess.run([self.exe, "-w", str(W), "-h", str(H), str(path), str(Path(self.tmp.name) / "out.png")],
                           capture_output=True, timeout=TIMEOUT, check=True)
        except subprocess.TimeoutExpired:
            return None
        return time.perf_counter() - t

    def close(self):
        self.tmp.cleanup()


class Selenium:
    def __init__(self):
        self.driver = self.start()
        self.driver.set_script_timeout(TIMEOUT)
        self.version = self.driver.capabilities.get("browserVersion", "?")

    def render(self, base, path):
        self.driver.get(base + "/")
        s = self.driver.execute_async_script(
            "const done = arguments[arguments.length - 1];"
            f"(async (url, w, h) => {{ {RENDER_JS} }})(...arguments).then(done, () => done(-1));",
            base + "/" + quote(path.name), W, H)
        return None if s < 0 else s

    def close(self):
        self.driver.quit()


class Firefox(Selenium):
    name = "Firefox (esvigy)"

    def start(self):
        from selenium import webdriver
        opts = webdriver.FirefoxOptions()
        opts.add_argument("--headless")
        opts.add_argument(f"--width={W}")
        opts.add_argument(f"--height={H}")
        return webdriver.Firefox(options=opts)


class Chromium(Selenium):
    name = "Chromium"

    def start(self):
        from selenium import webdriver
        opts = webdriver.ChromeOptions()
        opts.add_argument("--headless=new")
        opts.add_argument(f"--window-size={W},{H}")
        return webdriver.Chrome(options=opts)


class WebKit:
    name = "WebKitGTK"

    def __init__(self):
        cmd = ["/usr/bin/python3", "-c", WEBKIT]
        if shutil.which("xvfb-run"):
            cmd = ["xvfb-run", "-a", "-s", f"-screen 0 {W}x{H}x24"] + cmd
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.version = self.proc.stdout.readline().strip()
        if not self.version:
            raise RuntimeError("WebKitGTK didn't start (needs PyGObject with WebKit2 4.1)")

    def render(self, base, path):
        self.proc.stdin.write(f"{base} {base}/{quote(path.name)}\n")  # quoted: no spaces
        self.proc.stdin.flush()
        s = float(self.proc.stdout.readline())
        return None if s < 0 else s

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(10)


def main():
    files = []
    for arg in map(Path, sys.argv[1:]):
        files += sorted(arg.glob("*.svg")) if arg.is_dir() else [arg]
    # One server per folder, each serving only the files measured from it.
    servers = {d: serve(d, [f.name for f in files if f.parent == d]) for d in {f.parent for f in files}}
    bases = {d: f"http://127.0.0.1:{srv.server_port}" for d, srv in servers.items()}
    engines = []
    for cls in (Librsvg, Resvg, Firefox, Chromium, WebKit):
        try:
            engines.append(cls())
        except Exception as e:
            print(f"skipping {cls.name}: {e}".splitlines()[0], file=sys.stderr)
    try:
        print("versions: " + ", ".join(f"{e.name.split(' (')[0]} {e.version}" for e in engines) + "\n")
        print("| file | size | " + " | ".join(e.name for e in engines) + " |")
        print("| --- | ---: |" + " ---: |" * len(engines))
        for f in files:
            cells = []
            for e in engines:
                runs = [e.render(bases[f.parent], f) for _ in range(RUNS)]
                cells.append(f">{TIMEOUT} s / failed" if None in runs else f"{statistics.median(runs):.2f} s")
            print(f"| {f.name} | {f.stat().st_size / 1e6:.1f} MB | " + " | ".join(cells) + " |", flush=True)
    finally:
        for e in engines:
            e.close()
        for srv in servers.values():
            srv.shutdown()


if __name__ == "__main__":
    main()
