#!/usr/bin/env python3
"""Time how long it takes to get a big SVG on screen: librsvg (what eog/xviewer use) vs Firefox.

    benchmark/gen.py heavy /tmp/heavy
    .venv/bin/python benchmark/bench.py /tmp/heavy

librsvg is driven through GdkPixbuf in a subprocess of the system python3 (needs PyGObject), exactly
the call image viewers and file-manager thumbnailers make. Firefox is the headless one from selenium,
loading the file in an <img> and waiting for decode(), which is what esvigy's viewer does.
Both render to the same 1600x1200 pixels. Each file is run RUNS times; the median is reported.
"""

import functools
import http.server
import statistics
import subprocess
import sys
import threading
from pathlib import Path

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


def librsvg(path):
    try:
        out = subprocess.run(["/usr/bin/python3", "-c", PIXBUF, str(path)], capture_output=True, text=True,
                             timeout=TIMEOUT, check=True).stdout
        return float(out)
    except subprocess.TimeoutExpired:
        return None


def firefox_driver():
    from selenium import webdriver
    opts = webdriver.FirefoxOptions()
    opts.add_argument("--headless")
    opts.add_argument(f"--width={W}")
    opts.add_argument(f"--height={H}")
    return webdriver.Firefox(options=opts)


def serve(folder):
    """Plain local HTTP server: a page loaded from file:// or about:blank may not load other files."""
    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = functools.partial(Handler, directory=str(folder))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def firefox(driver, base, path):
    driver.get(base + "/")
    return driver.execute_async_script("""
        const done = arguments[arguments.length - 1];
        const img = new Image();
        img.width = arguments[1]; img.height = arguments[2];
        const t = performance.now();
        img.src = arguments[0];
        img.decode().then(() => { document.body.appendChild(img); requestAnimationFrame(() =>
            requestAnimationFrame(() => done((performance.now() - t) / 1000))); }, e => done(-1));
    """, base + "/" + path.name, W, H)


def main():
    files = sorted(Path(sys.argv[1]).glob("*.svg"))
    srv = serve(Path(sys.argv[1]))
    base = f"http://127.0.0.1:{srv.server_port}"
    driver = firefox_driver()
    try:
        print(f"| file | size | librsvg (eog, xviewer) | Firefox (esvigy) | speed-up |")
        print("| --- | ---: | ---: | ---: | ---: |")
        for f in files:
            lib = [librsvg(f) for _ in range(RUNS)]
            ff = [firefox(driver, base, f) for _ in range(RUNS)]
            lib_s = None if None in lib else statistics.median(lib)
            ff_s = statistics.median(ff)
            lib_txt = f">{TIMEOUT} s (gave up)" if lib_s is None else f"{lib_s:.2f} s"
            ratio = f"{(lib_s or TIMEOUT) / ff_s:.0f}×" if lib_s else f">{TIMEOUT / ff_s:.0f}×"
            print(f"| {f.name} | {f.stat().st_size / 1e6:.1f} MB | {lib_txt} | {ff_s:.2f} s | {ratio} |", flush=True)
    finally:
        driver.quit()
        srv.shutdown()


if __name__ == "__main__":
    main()
