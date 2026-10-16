#!/usr/bin/env python3
"""Regenerate the README screenshots from generated demo drawings.

    .venv/bin/python docs/screenshots.py
"""

import sys
import tempfile
import time
import threading
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.support.ui import WebDriverWait

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "benchmark"))
import esvigy  # noqa: E402
import gen  # noqa: E402


def main():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        root = tmp / "drawings"
        root.mkdir()
        for name, make in gen.GALLERY:
            (root / f"{name}.svg").write_text(make())
        esvigy.CACHE_DIR = tmp / "cache"
        srv = esvigy.make_server(root)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        opts = webdriver.FirefoxOptions()
        opts.add_argument("--headless")
        opts.add_argument("--width=1400")
        opts.add_argument("--height=860")
        driver = webdriver.Firefox(options=opts)
        try:
            driver.get(srv.url)
            wait = WebDriverWait(driver, 30)
            wait.until(lambda d: d.execute_script(
                "const i=[...document.querySelectorAll('#grid .card img')];"
                "return i.length>0 && i.every(x => x.complete && x.naturalWidth>0)"))
            driver.save_screenshot(str(HERE / "grid.png"))

            driver.get(srv.url + "#file=clusters-v1.svg&vs=clusters-v2.svg")
            wait.until(lambda d: d.execute_script(
                "const p=[...document.querySelectorAll('.pane')].filter(p => !p.hidden);"
                "return p.length==2 && p.every(x => x.querySelector('.ready'))"))
            time.sleep(.5)
            driver.save_screenshot(str(HERE / "compare.png"))
        finally:
            driver.quit()
            srv.shutdown()


if __name__ == "__main__":
    main()
