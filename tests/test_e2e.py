"""Browser tests: drive the real UI in headless Firefox and headless Chromium.

Each browser is skipped unless selenium is installed and esvigy finds the browser (the same
lookup as for its own window). Run only these with `pytest -m e2e`, or everything else with
`pytest -m "not e2e"`; `-k firefox` / `-k chromium` picks one browser.
"""

import json
import os
import re
import shutil
import struct
import sys
import threading
import time
import zlib
from pathlib import Path
from urllib.parse import quote

import pytest

import esvigy
from conftest import SMALL, make_bmp, make_jpeg_header, make_pdf, make_png, make_tiff, make_tree, tiff_bytes

webdriver = pytest.importorskip("selenium.webdriver")
from selenium.webdriver.common.action_chains import ActionChains  # noqa: E402
from selenium.webdriver.common.by import By  # noqa: E402
from selenium.webdriver.common.keys import Keys  # noqa: E402
from selenium.webdriver.support.ui import WebDriverWait  # noqa: E402

pytestmark = pytest.mark.e2e


def firefox_driver(downloads):
    opts = webdriver.FirefoxOptions()
    opts.add_argument("--headless")
    opts.add_argument("--width=1300")
    opts.add_argument("--height=800")
    # Same preferences as esvigy's own app window, so security settings there are tested too.
    for name, value in re.findall(r'^user_pref\("([^"]+)", (.+)\);$', esvigy.USER_JS, re.M):
        opts.set_preference(name, json.loads(value))
    return webdriver.Firefox(options=opts)


def chromium_driver(downloads):
    opts = webdriver.ChromeOptions()
    exe = esvigy.find_chromium()
    # chromedriver drives only Chrome and Chromium themselves; for Edge, Brave or Vivaldi selenium
    # uses (and on first run downloads) Chrome for Testing, the same engine.
    if re.search(r"chrom(e|ium)", os.path.basename(exe), re.I):
        opts.binary_location = exe
    opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1300,800")
    opts.add_experimental_option("prefs", {"download.default_directory": str(downloads),
                                           "download.prompt_for_download": False})
    return webdriver.Chrome(options=opts)


BROWSERS = {"firefox": (esvigy.find_firefox, firefox_driver), "chromium": (esvigy.find_chromium, chromium_driver)}


@pytest.fixture(scope="module", params=list(BROWSERS))
def app(request, tmp_path_factory):
    """One server and one browser for the whole module (starting a browser is slow), per browser."""
    find, start = BROWSERS[request.param]
    if not find():
        pytest.skip(f"{request.param} not installed")
    base = tmp_path_factory.mktemp("e2e")
    root = make_tree(base / "root")
    (root / "docs").mkdir()
    (root / "single").mkdir()
    (root / "live").mkdir()
    (root / "live" / "plot.svg").write_text(SMALL.format(w=200, h=100, c="teal"))
    (root / "live" / "other.svg").write_text(SMALL.format(w=200, h=100, c="gold"))
    (root / "single" / "only.svg").write_text(SMALL.format(w=120, h=80, c="navy"))
    make_pdf(root / "docs" / "a4.pdf", [(595, 842, (0, .5, .5))])
    make_pdf(root / "docs" / "two.pdf", [(842, 595, (.8, .1, .1)), (595, 842, (0, 0, 1))])
    make_pdf(root / "docs" / "three.pdf", [(595, 842, (0, .6, 0)), (595, 842, (1, .8, 0)), (595, 842, (.8, 0, .8))])
    # too much for one drawing slice: guards against thumbnails that never finish
    make_pdf(root / "docs" / "heavy.pdf", [(595, 842, (1, 1, 1))], shapes=150_000)
    (root / "pics").mkdir()
    make_png(root / "pics" / "a.png", 300, 200, (0, 128, 0))
    make_bmp(root / "pics" / "b.bmp", 30, 60, (0, 0, 255))
    make_tiff(root / "pics" / "scan.tif", [(400, 200, (255, 0, 0)), (200, 400, (0, 0, 255))])
    make_pdf(root / "pics" / "logo.ai", [(595, 842, (1, .5, 0))])
    old_cache, esvigy.CACHE_DIR = esvigy.CACHE_DIR, base / "cache"
    srv = esvigy.make_server(root)
    srv.pdf_inline = request.param == "firefox"  # as esvigy sets it for its app windows
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    (base / "downloads").mkdir()
    driver = start(base / "downloads")
    try:
        yield driver, srv.url, root, base / "cache", request.param, base / "downloads"
    finally:
        driver.quit()
        srv.shutdown()
        srv.server_close()
        esvigy.CACHE_DIR = old_cache


@pytest.fixture
def ui(app):
    """A fresh page with default preferences."""
    driver, url, root, cache, browser, downloads = app
    driver.get(url)
    driver.execute_script("localStorage.clear()")
    driver.get("about:blank")  # a real reload; driver.refresh() can hang here
    driver.get(url)
    ui = UI(driver, url, root, cache, browser, downloads)
    ui.wait(lambda: ui.card_names())
    return ui


class UI:
    def __init__(self, driver, url, root, cache, browser, downloads):
        self.d, self.url, self.root, self.cache = driver, url, root, cache
        self.browser, self.downloads = browser, downloads

    def wait(self, cond, timeout=20):
        return WebDriverWait(self.d, timeout).until(lambda _: cond())

    def js(self, script, *args):
        return self.d.execute_script(script, *args)

    def by_id(self, id_):
        return self.d.find_element(By.ID, id_)

    def cards(self):
        return self.d.find_elements(By.CSS_SELECTOR, "#grid .card")

    def card(self, name):
        return next(c for c in self.cards() if c.find_element(By.CSS_SELECTOR, ".name").text == name)

    def card_names(self):
        # One JS call, so a grid re-render can't leave us holding stale elements.
        return self.js("return [...document.querySelectorAll('#grid .card .name')].map(e => e.textContent)")

    def keys(self, *keys):
        ActionChains(self.d).send_keys(*keys).perform()

    def viewer_open(self):
        return not self.js("return document.getElementById('viewer').hidden")

    def visible_panes(self):
        return self.js("return [...document.querySelectorAll('.pane')].filter(p => !p.hidden).length")

    def wheel(self, pane, delta, times=1):
        self.js("""
            const s = document.querySelectorAll('.stage')[arguments[0]];
            const r = s.getBoundingClientRect();
            for (let i = 0; i < arguments[2]; i++)
              s.dispatchEvent(new WheelEvent('wheel', {deltaY: arguments[1], clientX: r.left + 100,
                                                       clientY: r.top + 100, bubbles: true, cancelable: true}));
        """, pane, delta, times)

    def hash(self):
        return self.js("return decodeURIComponent(location.hash)")


def test_grid_lists_files_and_renders_thumbnails(ui):
    assert ui.card_names() == ["a.svg", "b.svg", "viewbox-only.svg"]
    ui.wait(lambda: ui.js("return [...document.querySelectorAll('.card img')].every(i => i.getAttribute('src'))"))
    assert len(list(ui.cache.glob("*.webp"))) >= 3  # stored for next time


def test_open_step_and_close(ui):
    ui.card("a.svg").click()
    ui.wait(ui.viewer_open)
    assert ui.by_id("v-name").text == "a.svg"
    assert "file=a.svg" in ui.hash()
    ui.wait(lambda: ui.by_id("v-time").text.endswith("ms"))  # full render finished

    ui.keys(Keys.ARROW_RIGHT)
    ui.wait(lambda: ui.by_id("v-name").text == "b.svg")
    ui.keys(Keys.ESCAPE)
    ui.wait(lambda: not ui.viewer_open())
    assert "file=" not in ui.hash()


def test_browser_back_closes_viewer(ui):
    ui.card("b.svg").click()
    ui.wait(ui.viewer_open)
    ui.d.back()
    ui.wait(lambda: not ui.viewer_open())


def test_zoom_buttons(ui):
    ui.card("a.svg").click()
    ui.wait(ui.viewer_open)
    ui.by_id("v-100").click()
    assert ui.by_id("v-zoom").text == "100%"
    ui.wheel(0, -100, times=3)
    ui.wait(lambda: ui.by_id("v-zoom").text != "100%")
    ui.by_id("v-fit").click()
    assert ui.by_id("v-zoom").text != "100%"


def test_folder_tree_and_filter(ui):
    row = next(r for r in ui.d.find_elements(By.CSS_SELECTOR, "#tree .row") if r.text.strip().endswith("sub"))
    row.find_element(By.CSS_SELECTOR, ".label").click()
    ui.wait(lambda: ui.card_names() == ["mm.svg", "zipped.svgz"])
    assert "dir=sub" in ui.hash()
    assert ui.by_id("crumbs").text.replace(" ", "") == "root/sub"

    ui.by_id("filter").send_keys("zip")
    ui.wait(lambda: ui.card_names() == ["zipped.svgz"])


def test_compare_two_files_with_linked_zoom(ui):
    ActionChains(ui.d).key_down(Keys.CONTROL).click(ui.card("a.svg")).click(ui.card("b.svg")) \
        .key_up(Keys.CONTROL).perform()
    assert not ui.by_id("g-cmp").get_attribute("hidden")
    ui.by_id("g-cmp").click()
    ui.wait(lambda: ui.visible_panes() == 2)
    assert ui.by_id("v-name").text == "a.svg ⇄ b.svg"
    assert "vs=b.svg" in ui.hash()

    # linked: zooming one pane zooms both, so there is one shared zoom value
    before = ui.by_id("v-zoom").text
    ui.wheel(0, -100, times=3)
    ui.wait(lambda: ui.by_id("v-zoom").text != before)
    assert "|" not in ui.by_id("v-zoom").text

    # unlinked: only the pane under the cursor zooms
    ui.by_id("v-link").click()
    ui.wheel(1, -100, times=3)
    ui.wait(lambda: "|" in ui.by_id("v-zoom").text)

    ui.by_id("v-layout").click()
    assert "stack" in ui.by_id("panes").get_attribute("class")

    ui.keys("c")  # back to a single file
    ui.wait(lambda: ui.visible_panes() == 1)
    assert ui.by_id("v-name").text == "a.svg"


def test_compare_deep_link(ui):
    ui.d.get(ui.url + "#file=viewbox-only.svg&vs=a.svg")
    ui.wait(lambda: ui.by_id("v-name").text == "viewbox-only.svg ⇄ a.svg")
    assert ui.visible_panes() == 2


def pasted(ui):
    """What the clipboard holds, pasted into the filter field (reading it directly needs a permission)."""
    f = ui.by_id("filter")
    f.clear()
    f.send_keys(Keys.CONTROL, "v")
    text = f.get_attribute("value")
    f.clear()
    return text


def test_copy_full_paths(ui):
    root = str(ui.root.resolve())
    ui.by_id("copy-path").click()
    ui.wait(lambda: ui.by_id("copy-path").text == "✓")
    assert pasted(ui) == root
    ui.d.get(ui.url + "#dir=sub&file=mm.svg")
    ui.wait(lambda: ui.by_id("v-name").text == "mm.svg")
    ui.by_id("v-copy").click()
    ui.wait(lambda: ui.by_id("v-copy").text == "✓")
    assert pasted(ui) == os.path.join(root, "sub", "mm.svg")


def test_copy_both_paths_in_compare_mode(ui):
    ui.d.get(ui.url + "#file=a.svg&vs=b.svg")
    ui.wait(lambda: ui.by_id("v-name").text == "a.svg ⇄ b.svg")
    ui.by_id("v-copy").click()
    ui.wait(lambda: ui.by_id("v-copy").text == "✓")
    # an <input> drops line breaks on paste, so read the clipboard through a textarea
    ui.js("""const t = document.createElement("textarea"); t.id = "paste"; document.body.append(t);""")
    t = ui.by_id("paste")
    t.send_keys(Keys.CONTROL, "v")
    root = str(ui.root.resolve())
    assert t.get_attribute("value") == os.path.join(root, "a.svg") + "\n" + os.path.join(root, "b.svg")



@pytest.fixture
def clipboard(monkeypatch):
    """What /api/copy would put on the clipboard; the server runs in this process."""
    runs = []
    monkeypatch.setattr(esvigy, "clipboard_cmd", lambda: (["/usr/bin/xclip"], False))
    monkeypatch.setattr(esvigy.subprocess, "run", lambda args, input, **k: runs.append(input.decode()))
    return runs


def test_ctrl_c_copies_the_files_themselves(ui, clipboard):
    uri = lambda *p: ui.root.resolve().joinpath(*p).as_uri()  # noqa: E731
    ui.card("a.svg").send_keys(Keys.CONTROL, "c")  # the focused card
    ui.wait(lambda: clipboard == ["copy\n" + uri("a.svg")])
    assert "paste in a file manager" in ui.by_id("note").text

    ActionChains(ui.d).key_down(Keys.CONTROL).click(ui.card("a.svg")).click(ui.card("b.svg")) \
        .key_up(Keys.CONTROL).perform()
    ui.card("b.svg").send_keys(Keys.CONTROL, "c")  # one of the selected: both go
    ui.wait(lambda: len(clipboard) == 2)
    assert clipboard[1] == f"copy\n{uri('a.svg')}\n{uri('b.svg')}"

    ui.d.get(ui.url + "#dir=sub&file=mm.svg")
    ui.wait(lambda: ui.by_id("v-name").text == "mm.svg")
    ActionChains(ui.d).key_down(Keys.CONTROL).send_keys("c").key_up(Keys.CONTROL).perform()
    ui.wait(lambda: len(clipboard) == 3)
    assert clipboard[2] == "copy\n" + uri("sub", "mm.svg")


def test_ctrl_c_while_typing_copies_text_not_files(ui, clipboard):
    f = ui.by_id("filter")
    f.send_keys("a.svg", Keys.CONTROL, "a")
    f.send_keys(Keys.CONTROL, "c")
    time.sleep(0.3)
    assert clipboard == []


def test_dragging_a_card_drags_its_path(ui):
    data = ui.js("""const dt = new DataTransfer();
                    const img = arguments[0].querySelector("img");
                    img.dispatchEvent(new DragEvent("dragstart", { bubbles: true, cancelable: true, dataTransfer: dt }));
                    return [img.draggable, dt.types, dt.getData("text/plain")];""", ui.card("a.svg"))
    assert data == [False, ["text/plain"], str(ui.root.resolve() / "a.svg")]


def pasted_text(ui):
    """The clipboard, pasted into a textarea (an <input> would drop line breaks)."""
    ui.js("""document.getElementById("paste")?.remove();
             const t = document.createElement("textarea"); t.id = "paste"; document.body.append(t);""")
    t = ui.by_id("paste")
    t.send_keys(Keys.CONTROL, "v")
    text = t.get_attribute("value")
    ui.js('document.getElementById("paste").remove()')
    return text


# A newline runs the rest of a pasted name as a command; ESC[201~ ends bracketed paste first.
EVIL_NAME = "plot.svg\n\x1b[201~echo pwned\n.svg"
SMALL_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>'


@pytest.fixture
def scratch_dir(ui):
    """Make a folder in the shared test root, and remove it afterwards (other tests list the root)."""
    made = []

    def make(name):
        d = ui.root / name
        d.mkdir()
        made.append(d)
        return d

    yield make
    for d in made:
        shutil.rmtree(d, ignore_errors=True)


def test_paths_with_control_characters_are_not_copied(ui, scratch_dir):
    d = scratch_dir("ctl")
    (d / "safe ü.svg").write_text(SMALL_SVG)
    (d / EVIL_NAME).write_text(SMALL_SVG)
    safe = os.path.join(str(ui.root.resolve()), "ctl", "safe ü.svg")

    ui.d.get(ui.url + "#dir=ctl&file=" + quote("safe ü.svg"))
    ui.wait(lambda: ui.by_id("v-name").text == "safe ü.svg")
    ui.by_id("v-copy").click()
    ui.wait(lambda: ui.by_id("v-copy").text == "✓")

    ui.d.get(ui.url + "#dir=ctl&file=" + quote(EVIL_NAME))
    ui.wait(lambda: ui.js("return document.getElementById('v-name').textContent") == EVIL_NAME)
    ui.by_id("v-copy").click()
    ui.wait(lambda: ui.by_id("v-copy").text == "✗")
    assert "Not copied" in ui.by_id("v-copy").get_attribute("title")
    assert pasted_text(ui) == safe  # the clipboard still holds the safe path

    # in compare mode, one bad name is enough to refuse both
    ui.d.get(ui.url + "#dir=ctl&file=" + quote("safe ü.svg") + "&vs=" + quote(EVIL_NAME))
    ui.wait(lambda: "⇄" in ui.by_id("v-name").text)
    ui.wait(lambda: ui.by_id("v-copy").text == "⧉", timeout=10)
    ui.by_id("v-copy").click()
    ui.wait(lambda: ui.by_id("v-copy").text == "✗")
    assert pasted_text(ui) == safe


def test_folder_with_control_characters_is_not_copied(ui, scratch_dir):
    bad = "bad\x1b]0;title\x07dir"  # an escape code in a folder name
    (scratch_dir(bad) / "x.svg").write_text(SMALL_SVG)
    ui.d.get(ui.url + "#dir=" + quote(bad))
    ui.wait(lambda: not ui.by_id("copy-path").get_attribute("hidden") and "x.svg" in ui.card_names())
    ui.by_id("copy-path").click()
    ui.wait(lambda: ui.by_id("copy-path").text == "✗")


def test_table_view_sorts_by_column(ui):
    ui.keys("t")
    ui.wait(lambda: ui.by_id("thead").is_displayed())
    sizes = {p.name: p.stat().st_size for p in ui.root.glob("*.svg") if not p.name.startswith(".")}
    by_size = sorted(sizes, key=lambda n: -sizes[n])

    ui.d.find_element(By.CSS_SELECTOR, "#thead [data-sort=size]").click()
    ui.wait(lambda: ui.card_names() == by_size)
    ui.d.find_element(By.CSS_SELECTOR, "#thead [data-sort=size]").click()  # again: reversed
    ui.wait(lambda: ui.card_names() == by_size[::-1])

    ui.card(by_size[0]).click()  # rows open the viewer like cards do
    ui.wait(ui.viewer_open)


# ---------------------------------------------------------------- security in a real browser

EVIL_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"
  onload="document.title='pwned-onload'">
  <script>document.title = 'pwned-script'; fetch('../api/list').then(() => document.title = 'pwned-fetch');</script>
  <image href="http://127.0.0.1:9/track.png" width="1" height="1"/>
</svg>"""


def test_svg_scripts_never_run(ui):
    (ui.root / "evil.svg").write_text(EVIL_SVG)
    ui.by_id("refresh").click()
    ui.wait(lambda: "evil.svg" in ui.card_names())
    ui.card("evil.svg").click()  # in the viewer (<img>)
    ui.wait(lambda: ui.by_id("v-time").text.endswith("ms"))
    assert "pwned" not in ui.d.title

    ui.d.get(ui.url + "file/evil.svg")  # opened directly, like the "raw ↗" link
    ui.wait(lambda: ui.js("return document.readyState") == "complete")
    assert "pwned" not in ui.d.title
    (ui.root / "evil.svg").unlink()



def test_pdf_scripts_never_run_when_opened_raw(ui):
    # The browser's own PDF viewer ignores our CSP. In Firefox the app profile's preference stops
    # PDF scripts; Chromium has no such switch, so there the PDF is downloaded instead of shown.
    from selenium.common.exceptions import NoAlertPresentException
    (ui.root / "evil").mkdir(exist_ok=True)
    make_pdf(ui.root / "evil" / "js.pdf", [(595, 842, (1, 1, 1))], js=b"app.alert('pwned-pdf');")
    if ui.browser == "chromium":
        ui.d.get(ui.url + "file/evil/js.pdf")
        ui.wait(lambda: (ui.downloads / "js.pdf").is_file())
        assert ui.js("return location.href").startswith(ui.url)  # still the UI, no viewer
        with pytest.raises(NoAlertPresentException):
            ui.d.switch_to.alert.dismiss()
        return
    ui.d.get(ui.url + "file/evil/js.pdf")
    ui.wait(lambda: ui.js("return !!window.PDFViewerApplication?.pdfDocument"))
    ui.wait(lambda: ui.js("return document.querySelector('.page canvas') != null"))
    time.sleep(1.5)  # an OpenAction script would have run by now
    with pytest.raises(NoAlertPresentException):
        ui.d.switch_to.alert.dismiss()

def test_page_without_token_shows_nothing(ui):
    base = ui.url.rsplit("/", 2)[0] + "/"  # http://127.0.0.1:PORT/
    ui.d.get(base)
    assert "esvigy" not in ui.d.title and not ui.d.find_elements(By.ID, "grid")


def test_no_csp_violations_in_the_ui(ui):
    # The CSP must not break the app itself (renderer frame, blob thumbnails, styles).
    ui.js("window._violations = []; document.addEventListener('securitypolicyviolation', "
          "e => window._violations.push(e.violatedDirective + ' ' + e.blockedURI))")
    for p in ui.cache.glob("*.webp"):  # force real renders through the renderer frame
        p.unlink()
    ui.by_id("refresh").click()
    ui.wait(lambda: ui.js("return [...document.querySelectorAll('.card img')].every(i => i.getAttribute('src'))"))
    ui.card("a.svg").click()
    ui.wait(lambda: ui.by_id("v-time").text.endswith("ms"))
    assert ui.js("return window._violations") == []


# ---------------------------------------------------------------- PDF

def pane_pixel(ui, fx, fy):
    """RGB of the visible PDF canvas in pane 0 at fractions (fx, fy) of its size."""
    return ui.js("""
        const c = document.querySelector('.pane:not([hidden]) canvas.ready');
        if (!c) return null;
        const d = c.getContext('2d').getImageData(Math.floor(c.width * arguments[0]),
                                                  Math.floor(c.height * arguments[1]), 1, 1).data;
        return [d[0], d[1], d[2]];
    """, fx, fy)


def test_pdf_thumbnails_and_page_info(ui):
    ui.d.get(ui.url + "#dir=docs")
    ui.wait(lambda: ui.card_names() == ["a4.pdf", "heavy.pdf", "three.pdf", "two.pdf"])
    ui.wait(lambda: ui.js("return [...document.querySelectorAll('.card img')].every(i => i.getAttribute('src'))"),
            timeout=60)
    # the browser measured the PDFs: real page size and page count replace the placeholder
    dims = lambda: ui.js("return [...document.querySelectorAll('.card .dims')].map(e => e.textContent)")
    ui.wait(lambda: dims() == ["793×1123 · 1 page", "793×1123 · 1 page", "793×1123 · 3 pages", "1123×793 · 2 pages"])
    assert not ui.d.find_elements(By.CSS_SELECTOR, ".card .note")  # no "cannot render"
    assert len(list(ui.cache.glob("*.json"))) >= 4


def test_pdf_viewer_draws_the_page(ui):
    ui.d.get(ui.url + "#dir=docs&file=a4.pdf")
    ui.wait(lambda: ui.by_id("v-time").text.endswith("ms"))
    assert ui.by_id("v-info").text.startswith("793×1123 · 1 page")
    assert pane_pixel(ui, 0.5, 0.5) == pytest.approx([0, 128, 128], abs=2)  # page colour
    assert pane_pixel(ui, 0.05, 0.03) == [0, 0, 0]  # black mark: top-left, not upside down


def test_pdf_redraws_sharp_after_zoom(ui):
    ui.d.get(ui.url + "#dir=docs&file=two.pdf")
    ui.wait(lambda: ui.by_id("v-time").text.endswith("ms"))
    width = lambda: ui.js("return document.querySelector('.pane:not([hidden]) canvas.ready').width")
    before = width()
    ui.wheel(0, -100, times=4)
    ui.wait(lambda: width() > before * 1.5)  # a new, bigger bitmap after the zoom settles
    assert pane_pixel(ui, 0.5, 0.5) == pytest.approx([204, 26, 26], abs=2)


def test_compare_pdf_with_svg(ui):
    ui.d.get(ui.url + "#dir=docs&file=a4.pdf&vs=two.pdf")
    ui.wait(lambda: ui.visible_panes() == 2)
    ui.wait(lambda: ui.js("return document.querySelectorAll('.pane:not([hidden]) canvas.ready').length") == 2)


def test_compare_step_each_pane_or_both(ui):
    # files: a.svg, b.svg, viewbox-only.svg
    ui.d.get(ui.url + "#file=a.svg&vs=b.svg")
    ui.wait(lambda: ui.visible_panes() == 2)
    name = lambda: ui.by_id("v-name").text
    cap_button = lambda pane, i: ui.d.find_elements(By.CSS_SELECTOR, ".pane")[pane] \
        .find_elements(By.CSS_SELECTOR, ".cap button")[i]

    cap_button(0, 1).click()  # A: next, onto the file B shows (same figure on both sides is fine)
    ui.wait(lambda: name() == "b.svg ⇄ b.svg")
    cap_button(1, 0).click()  # B: previous
    ui.wait(lambda: name() == "b.svg ⇄ a.svg")

    ui.by_id("v-next").click()  # top bar: both panes
    ui.wait(lambda: name() == "viewbox-only.svg ⇄ b.svg")
    ui.keys(Keys.ARROW_DOWN)  # both, wrapping around
    ui.wait(lambda: name() == "a.svg ⇄ viewbox-only.svg")
    ui.keys(Keys.ARROW_UP)
    ui.wait(lambda: name() == "viewbox-only.svg ⇄ b.svg")
    ui.keys(Keys.ARROW_RIGHT)  # unchanged: B only
    ui.wait(lambda: name() == "viewbox-only.svg ⇄ viewbox-only.svg")


def pane_state(ui, i):
    """Page label, layer aspect (w/h) and centre colour of visible pane `i`."""
    return ui.js("""
        const p = [...document.querySelectorAll('.pane')].filter(p => !p.hidden)[arguments[0]];
        if (!p) return { label: null, aspect: null, rgb: null };
        const c = p.querySelector('canvas.ready'), l = p.querySelector('.layer');
        let rgb = null;
        if (c) { const d = c.getContext('2d').getImageData(c.width >> 1, c.height >> 1, 1, 1).data;
                 rgb = [d[0], d[1], d[2]]; }
        return { label: p.classList.contains('paged') ? p.querySelector('.pbar span').textContent : null,
                 aspect: l.offsetWidth / l.offsetHeight, rgb };
    """, i)


def test_pdf_pages(ui):
    ui.d.get(ui.url + "#dir=docs&file=two.pdf")
    ui.wait(lambda: pane_state(ui, 0)["rgb"] == [204, 26, 26])
    st = pane_state(ui, 0)
    assert st["label"] == "1 / 2" and st["aspect"] > 1  # page 1: landscape, red

    ui.keys(Keys.PAGE_DOWN)
    ui.wait(lambda: pane_state(ui, 0)["rgb"] == [0, 0, 255])
    st = pane_state(ui, 0)
    assert st["label"] == "2 / 2" and st["aspect"] < 1  # page 2: portrait, blue
    assert "page=2" in ui.hash()

    ui.keys(Keys.PAGE_DOWN)  # last page: stays (does not jump to another file)
    assert "file=two.pdf" in ui.hash() and pane_state(ui, 0)["label"] == "2 / 2"

    ui.d.find_element(By.CSS_SELECTOR, ".pane .pbar button").click()  # ‹
    ui.wait(lambda: pane_state(ui, 0)["label"] == "1 / 2")
    assert "page=" not in ui.hash()


def test_pdf_page_deep_link_and_file_step_resets_page(ui):
    ui.d.get(ui.url + "#dir=docs&file=three.pdf&page=3")
    ui.wait(lambda: pane_state(ui, 0)["rgb"] == [204, 0, 204])
    ui.keys(Keys.ARROW_RIGHT)  # next file starts at page 1
    ui.wait(lambda: ui.by_id("v-name").text == "two.pdf")
    assert "page=" not in ui.hash()


def test_compare_turns_pages_together_or_per_pane(ui):
    ui.d.get(ui.url + "#dir=docs&file=two.pdf&vs=three.pdf")
    ui.wait(lambda: pane_state(ui, 1)["label"] == "1 / 3")

    ui.keys(Keys.PAGE_DOWN)  # both
    ui.wait(lambda: (pane_state(ui, 0)["label"], pane_state(ui, 1)["label"]) == ("2 / 2", "2 / 3"))
    ui.keys(Keys.PAGE_DOWN)  # A is at its end, B goes on
    ui.wait(lambda: (pane_state(ui, 0)["label"], pane_state(ui, 1)["label"]) == ("2 / 2", "3 / 3"))
    assert "page=2" in ui.hash() and "vspage=3" in ui.hash()

    pb = ui.d.find_elements(By.CSS_SELECTOR, ".pane")[1].find_elements(By.CSS_SELECTOR, ".pbar button")
    pb[0].click()  # B only
    ui.wait(lambda: (pane_state(ui, 0)["label"], pane_state(ui, 1)["label"]) == ("2 / 2", "2 / 3"))
    ui.wait(lambda: pane_state(ui, 1)["rgb"] == [255, 204, 0])


def test_page_keys_step_files_without_multipage_pdf(ui):
    ui.d.get(ui.url + "#file=a.svg")
    ui.wait(ui.viewer_open)
    ui.keys(Keys.PAGE_DOWN)
    ui.wait(lambda: ui.by_id("v-name").text == "b.svg")


# ---------------------------------------------------------------- raster images, TIFF, AI

def test_images_get_thumbnails_and_coloured_type_badges(ui):
    ui.d.get(ui.url + "#dir=pics")
    ui.wait(lambda: ui.card_names() == ["a.png", "b.bmp", "logo.ai", "scan.tif"])
    ui.wait(lambda: ui.js("return [...document.querySelectorAll('.card img')].every(i => i.getAttribute('src'))"),
            timeout=60)
    assert not ui.d.find_elements(By.CSS_SELECTOR, ".card .note")  # no "cannot render"
    badges = ui.js("""return [...document.querySelectorAll('.card .pic')].map(p => {
        const s = getComputedStyle(p, '::before');
        return [s.content, s.backgroundColor]; })""")
    assert [b[0] for b in badges] == ['"PNG"', '"BMP"', '"AI"', '"TIFF"']
    assert len({b[1] for b in badges}) == 4  # each format its own colour
    # the TIFF's page count is known once it was decoded
    dims = lambda: ui.js("return [...document.querySelectorAll('.card .dims')].map(e => e.textContent)")
    ui.wait(lambda: dims() == ["300×200", "30×60", "793×1123 · 1 page", "400×200 · 2 pages"])


def test_raster_image_viewer(ui):
    ui.d.get(ui.url + "#dir=pics&file=a.png")
    ui.wait(lambda: ui.by_id("v-time").text.endswith("ms"))
    assert ui.by_id("v-info").text.startswith("300×200")
    assert ui.js("return document.querySelector('.pane .img.ready').naturalWidth") == 300
    assert pane_state(ui, 0)["aspect"] == pytest.approx(1.5, abs=0.02)


def test_tiff_pages(ui):
    ui.d.get(ui.url + "#dir=pics&file=scan.tif")
    ui.wait(lambda: pane_state(ui, 0)["rgb"] == [255, 0, 0])
    st = pane_state(ui, 0)
    assert st["label"] == "1 / 2" and st["aspect"] > 1
    ui.keys(Keys.PAGE_DOWN)
    ui.wait(lambda: pane_state(ui, 0)["rgb"] == [0, 0, 255])
    st = pane_state(ui, 0)
    assert st["label"] == "2 / 2" and st["aspect"] < 1
    ui.wheel(0, -100, times=4)  # redraws after zooming must not break the page
    time.sleep(0.5)
    assert pane_state(ui, 0)["rgb"] == [0, 0, 255]


def test_illustrator_file_is_drawn_as_pdf(ui):
    ui.d.get(ui.url + "#dir=pics&file=logo.ai")
    ui.wait(lambda: ui.by_id("v-time").text.endswith("ms"))
    assert pane_pixel(ui, 0.5, 0.5) == pytest.approx([255, 128, 0], abs=2)


def test_compare_png_with_tiff(ui):
    ui.d.get(ui.url + "#dir=pics&file=a.png&vs=scan.tif")
    ui.wait(lambda: ui.visible_panes() == 2)
    ui.wait(lambda: pane_state(ui, 1)["rgb"] == [255, 0, 0])
    assert ui.js("return document.querySelector('.pane .img.ready') != null")



# ---------------------------------------------------------------- hostile TIFFs
# UTIF.js trusts every size a TIFF claims. tiffcheck.js and the guards in tiffworker.js must
# refuse these before UTIF allocates. Each case is small enough that, without its guard, the
# test fails (wrong outcome) instead of exhausting memory. The limit is lowered to 1 MP here.

LIMIT = 1e6
NIKON_LOOP = b"Nikon\0\2\0\0\0" + b"II*\0" + struct.pack("<IHI", 8, 0, 8)


def put_tiff(ui, name, data):
    (ui.root / "tiffsec").mkdir(exist_ok=True)
    (ui.root / "tiffsec" / name).write_bytes(data)
    return ui.url + "file/tiffsec/" + name


def decode_tiff(ui, url, max_pixels=LIMIT):
    """Decode a TIFF's first page in the real tiffworker.js: its size and centre pixel, or the error.

    (Test scripts run in a sandbox that can't see the page's `const tiffDocs`, so this talks
    to the worker directly, which is where all the limits are applied.)
    """
    ui.d.set_script_timeout(30)
    return ui.d.execute_async_script("""
        const [url, maxPixels, done] = arguments;
        const w = new Worker("static/tiffworker.js");
        const call = (msg) => new Promise((ok) => { w.onmessage = (e) => ok(e.data); w.postMessage(msg); });
        (async () => {
          const opened = await call({ id: 1, open: url, maxPixels });
          if (opened.error) return opened;
          const p = await call({ id: 2, page: 1 });
          if (p.error) return p;
          const px = new Uint8Array(p.rgba, (((p.h >> 1) * p.w) + (p.w >> 1)) * 4, 3);
          return { size: [p.w, p.h], pixel: [...px] };
        })().then((r) => { w.terminate(); done(r); });
    """, url, max_pixels)


def check_structure(ui, url):
    """Run only tiffLimits.checkStructure (no UTIF): for files UTIF itself would never finish."""
    ui.d.set_script_timeout(30)
    return ui.d.execute_async_script("""
        const [url, done] = arguments;
        (async () => {
          let limits;
          try {
            const src = await (await fetch("static/tiffcheck.js")).text();
            limits = new Function(src + "; return tiffLimits;")();
          } catch (e) {
            // Chromium applies the page's CSP (no eval) to test scripts too, but unlike Firefox it
            // lets them see the page's globals: load the file as a normal page script instead.
            if (typeof tiffLimits === "undefined") {
              await new Promise((ok, fail) => {
                const s = document.createElement("script");
                s.src = "static/tiffcheck.js"; s.onload = ok; s.onerror = fail;
                document.head.append(s);
              });
            }
            limits = tiffLimits;
          }
          const buf = await (await fetch(url)).arrayBuffer();
          try { limits.checkStructure(buf); return "ok"; }
          catch (e) { return e.name + ": " + e.message; }
        })().then(done);
    """, url)


def test_tiff_normal_files_still_decode(ui):
    gray = tiff_bytes(300, 200, bytes([200]) * 60000)
    assert decode_tiff(ui, put_tiff(ui, "gray.tif", gray)) == {"size": [300, 200], "pixel": [200, 200, 200]}
    # Deflate goes through the guarded inflate
    deflated = tiff_bytes(300, 200, zlib.compress(bytes([90]) * 60000), compression=8)
    assert decode_tiff(ui, put_tiff(ui, "deflate.tif", deflated)) == {"size": [300, 200], "pixel": [90, 90, 90]}
    assert check_structure(ui, put_tiff(ui, "gray.tif", gray)) == "ok"


@pytest.mark.parametrize("name, data, expect", [
    # claims 2 MP with 16 bytes of data
    ("dims.tif", lambda: tiff_bytes(2000, 1000, b"\0" * 16), "image too large"),
    # 0.5 MP, but 16 samples of 32 bits each: 32 MB of raw data
    ("bits.tif", lambda: tiff_bytes(1000, 500, b"\0" * 16, bits=(32,) * 16), "image data too large"),
    # a small image made of one 2000×2000 tile
    ("tiles.tif", lambda: tiff_bytes(100, 100, b"\0" * 16, extra=[(322, 4, 1, 2000), (323, 4, 1, 2000)]),
     "tiles too large"),
    # 10 KB of pixels, but its Deflate strip expands to 20 MB
    ("bomb.tif", lambda: tiff_bytes(100, 100, zlib.compress(b"\0" * 20_000_000, 9), compression=8), "expands"),
    # a small image whose embedded JPEG claims 3000×3000
    ("jpeg.tif", lambda: tiff_bytes(100, 100, make_jpeg_header(3000, 3000), compression=7), "embedded JPEG"),
    ("ljpeg.tif", lambda: tiff_bytes(100, 100, make_jpeg_header(3000, 3000), compression=7, photometric=32803,
                                     bits=(16,)), "embedded JPEG"),
])
def test_tiff_claims_are_refused_before_decoding(ui, name, data, expect):
    r = decode_tiff(ui, put_tiff(ui, name, data()))
    assert r.get("tooLarge") is True and expect in r["error"], r


def test_tiff_tag_counts_past_the_end_are_refused(ui):
    # 20 million SHORT values claimed in a 200-byte file; UTIF would loop over every one
    data = tiff_bytes(10, 10, b"\0" * 100, extra=[(305, 3, 20_000_000, 8)])
    r = decode_tiff(ui, put_tiff(ui, "count.tif", data))
    assert "outside the file" in r.get("error", ""), r


@pytest.mark.parametrize("name, data", [
    ("loop.tif", lambda: tiff_bytes(10, 10, b"\0" * 100, next_ifd=8)),        # next directory: itself
    ("subloop.tif", lambda: tiff_bytes(10, 10, b"\0" * 100, extra=[(330, 4, 1, 8)])),  # sub-image: itself
    ("exifloop.tif", lambda: tiff_bytes(10, 10, b"\0" * 100, extra=[(34665, 4, 1, 8)])),
    ("noteloop.tif", lambda: tiff_bytes(10, 10, b"\0" * 100, extra=[(37500, 7, 4, 8)])),  # maker note: itself
    # a Nikon maker note is a TIFF of its own; this one is an empty directory whose "next" is itself
    ("nikon.tif", lambda: tiff_bytes(10, 10, b"\0" * 100, extra=[(37500, 7, 24, NIKON_LOOP)])),
])
def test_tiff_directory_loops_are_refused(ui, name, data):
    # Only the check runs: UTIF itself would never finish on these.
    assert "loop" in check_structure(ui, put_tiff(ui, name, data()))


def test_tiff_too_large_is_explained_in_the_viewer(ui):
    # 400 MP claimed (default limit 300 MP) with almost no data: refused before anything is allocated
    (ui.root / "tiffbig").mkdir(exist_ok=True)
    (ui.root / "tiffbig" / "huge.tif").write_bytes(tiff_bytes(20000, 20000, b"\0" * 16))
    ui.d.get(ui.url + "#dir=tiffbig&file=huge.tif")
    ui.wait(lambda: "too large" in ui.js("return document.querySelector('.pane .load')?.textContent || ''"))


# ---------------------------------------------------------------- launching the app window

def firefox_tree(pid):
    """pid and all its descendants (Firefox's content processes), from /proc."""
    children = {}
    for stat_file in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat_file.read_text().rsplit(")", 1)[1].split()
        except OSError:
            continue
        children.setdefault(int(fields[1]), []).append(int(stat_file.parent.name))
    out, todo = [], [pid]
    while todo:
        p = todo.pop()
        out.append(p)
        todo += children.get(p, [])
    return out


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
@pytest.mark.parametrize("browser", ["app", "chromium"])
def test_real_app_window_keeps_the_token_off_command_lines(tmp_path, monkeypatch, browser):
    # Started the way main() does it: the browser gets the forwarding page's path, follows it to
    # the secret URL, and the token appears in no command line of any of its processes.
    mode, exe = esvigy.choose_browser(browser)
    if mode != {"app": "firefox-app", "chromium": "chromium-app"}[browser]:
        pytest.skip(f"no browser for -b {browser}")
    monkeypatch.setattr(esvigy, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(esvigy, "PROFILE_DIR", tmp_path / "data" / "firefox-profile")
    monkeypatch.setenv("MOZ_HEADLESS", "1")
    real_popen = esvigy.subprocess.Popen  # Chromium has no headless environment variable: add the flag
    monkeypatch.setattr(esvigy.subprocess, "Popen", lambda args, **kw: real_popen(
        args[:1] + ["--headless=new"] + args[1:] if mode == "chromium-app" else args, **kw))
    root = make_tree(tmp_path / "root")
    old_cache, esvigy.CACHE_DIR = esvigy.CACHE_DIR, tmp_path / "cache"
    srv = esvigy.make_server(root)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    page = srv.launch_page = esvigy.launch_page(srv.url)
    proc, profile = esvigy.open_browser(page.as_uri(), mode, exe)
    try:
        assert proc is not None
        deadline = time.time() + 60
        while page.exists() and time.time() < deadline:  # deleted when the UI is requested with the token
            time.sleep(0.1)
        assert not page.exists(), "the browser never arrived at the UI"
        cmdlines = {pid: Path(f"/proc/{pid}/cmdline").read_bytes() for pid in firefox_tree(proc.pid)
                    if Path(f"/proc/{pid}/cmdline").exists()}
        assert len(cmdlines) > 1  # the main process and at least one content process
        assert not [pid for pid, c in cmdlines.items() if srv.token.encode() in c], cmdlines
    finally:
        proc.terminate()
        if profile:
            esvigy.remove_profile(profile, proc)
        else:
            proc.wait(20)
        srv.shutdown()
        srv.server_close()
        esvigy.CACHE_DIR = old_cache


def layer_offset(ui, i=0):
    """The (x, y) translation of visible pane `i`'s layer."""
    return ui.js("""
        const p = [...document.querySelectorAll('.pane')].filter(p => !p.hidden)[arguments[0]];
        const m = new DOMMatrix(getComputedStyle(p.querySelector('.layer')).transform);
        return [Math.round(m.e), Math.round(m.f)];
    """, i)


def test_drag_pans_the_image(ui):
    # A real mouse drag on the image must pan it, not start the browser's "drag this image".
    ui.card("a.svg").click()
    ui.wait(lambda: ui.by_id("v-time").text.endswith("ms"))
    ui.wheel(0, -100, times=6)  # zoom in, so the image covers the stage
    ui.wait(lambda: "moving" not in ui.d.find_element(By.CSS_SELECTOR, ".pane .layer").get_attribute("class"))
    x0, y0 = layer_offset(ui)
    stage = ui.d.find_element(By.CSS_SELECTOR, ".pane .stage")
    actions = ActionChains(ui.d).move_to_element(stage).click_and_hold()
    for _ in range(6):
        actions.move_by_offset(-25, -15)
    actions.release().perform()
    x1, y1 = layer_offset(ui)
    assert (x0 - x1, y0 - y1) == (150, 90)


def test_drag_in_compare_pans_both_when_linked(ui):
    ui.d.get(ui.url + "#file=a.svg&vs=b.svg")
    ui.wait(lambda: ui.visible_panes() == 2)
    ui.wait(lambda: all(e.text.endswith("ms") for e in ui.d.find_elements(By.CSS_SELECTOR, ".pane .cap .ci span:last-child")))
    before = [layer_offset(ui, 0), layer_offset(ui, 1)]
    stage = ui.d.find_elements(By.CSS_SELECTOR, ".pane .stage")[0]
    ActionChains(ui.d).move_to_element(stage).click_and_hold().move_by_offset(40, 30) \
        .move_by_offset(40, 30).release().perform()
    after = [layer_offset(ui, 0), layer_offset(ui, 1)]
    assert [(a[0] - b[0], a[1] - b[1]) for a, b in zip(after, before)] == [(80, 60), (80, 60)]


def test_compare_two_parts_of_the_same_figure(ui):
    # Same file on both sides; unlinked, each side zooms (and pans) on its own.
    ui.d.get(ui.url + "#file=a.svg&vs=a.svg")
    ui.wait(lambda: ui.visible_panes() == 2)
    assert ui.by_id("v-name").text == "a.svg ⇄ a.svg"
    ui.wait(lambda: all(e.text.endswith("ms") for e in ui.d.find_elements(By.CSS_SELECTOR, ".pane .cap .ci span:last-child")))
    if "on" in ui.by_id("v-link").get_attribute("class"):
        ui.by_id("v-link").click()
    ui.wheel(1, -100, times=4)
    ui.wait(lambda: "|" in ui.by_id("v-zoom").text)  # two different zoom levels


def test_compare_in_a_folder_with_one_file(ui):
    ui.d.get(ui.url + "#dir=single")
    ui.wait(lambda: ui.card_names() == ["only.svg"])
    ui.card("only.svg").click()
    ui.wait(ui.viewer_open)
    ui.keys("c")
    ui.wait(lambda: ui.visible_panes() == 2)
    assert ui.by_id("v-name").text == "only.svg ⇄ only.svg"


# ---------------------------------------------------------------- files in the sidebar

def side_files(ui):
    """[(name, folder of the tree node it is listed under, highlighted, A/B mark)] of the sidebar's file rows."""
    return ui.js("""
        return [...document.querySelectorAll('#tree .row.file')].map(r => [
            r.querySelector('.label').textContent,
            r.closest('ul.files').parentElement.querySelector(':scope > .row .label').textContent,
            r.classList.contains('cur'), r.querySelector('.ab').textContent]);
    """)


def side_row(ui, name):
    # textContent, not .text: Selenium's .text is empty for rows of a folded folder
    return next(r for r in ui.d.find_elements(By.CSS_SELECTOR, "#tree .row.file")
                if r.find_element(By.CSS_SELECTOR, ".label").get_attribute("textContent") == name)


def files_in(ui, folder):
    return [n for n, f, *_ in side_files(ui) if f == folder]


def test_sidebar_lists_the_files_of_each_folder_shown(ui):
    ui.wait(lambda: [n for n, *_ in side_files(ui)] == ui.card_names())
    assert {folder for _, folder, *_ in side_files(ui)} == {"root"}
    ui.d.get(ui.url + "#dir=sub")
    ui.wait(lambda: files_in(ui, "sub") == ["mm.svg", "zipped.svgz"])
    assert files_in(ui, "root") == ["a.svg", "b.svg", "viewbox-only.svg"]  # still there, to compare with
    ui.by_id("filter").send_keys("zip")  # filters every folder's list
    ui.wait(lambda: [n for n, *_ in side_files(ui)] == ["zipped.svgz"])


def tree_node(ui, name):
    return next(r for r in ui.d.find_elements(By.CSS_SELECTOR, "#tree .row:not(.file)")
                if r.find_element(By.CSS_SELECTOR, ".label").get_attribute("textContent") == name)


def test_folder_opened_with_its_arrow_lists_its_files(ui):
    tree_node(ui, "single").find_element(By.CSS_SELECTOR, ".twisty").click()  # no subfolders, one file
    ui.wait(lambda: files_in(ui, "single") == ["only.svg"])
    assert "dir=single" not in ui.hash()  # the grid stays where it was
    assert tree_node(ui, "single").find_element(By.CSS_SELECTOR, ".twisty") \
        .value_of_css_property("visibility") == "visible"
    side_row(ui, "only.svg").click()  # opens it, in its folder
    ui.wait(lambda: ui.viewer_open() and ui.by_id("v-name").text == "only.svg")
    assert "dir=single" in ui.hash()


def test_compare_files_of_different_folders(ui):
    ui.d.get(ui.url + "#file=a.svg&vs=b.svg")
    ui.wait(lambda: ui.visible_panes() == 2 and side_files(ui))
    tree_node(ui, "sub").find_element(By.CSS_SELECTOR, ".twisty").click()
    ui.wait(lambda: files_in(ui, "sub") == ["mm.svg", "zipped.svgz"])
    side_row(ui, "mm.svg").click()  # side B, from another folder
    ui.wait(lambda: ui.by_id("v-name").text == "a.svg ⇄ /sub/mm.svg")
    assert "vsdir=sub" in ui.hash() and "dir=" not in ui.hash().replace("vsdir=", "")
    assert ui.by_id("v-pos").text == "A 1 / 3 · B 1 / 2"
    marks = lambda: {n: ab for n, _, _, ab in side_files(ui) if ab}
    assert marks() == {"a.svg": "A", "mm.svg": "B"}
    ui.keys(Keys.ARROW_RIGHT)  # B steps through its own folder
    ui.wait(lambda: ui.by_id("v-name").text == "a.svg ⇄ /sub/zipped.svgz")
    ActionChains(ui.d).key_down(Keys.SHIFT).send_keys(Keys.ARROW_RIGHT).key_up(Keys.SHIFT).perform()  # A: its folder
    ui.wait(lambda: ui.by_id("v-name").text == "b.svg ⇄ /sub/zipped.svgz")
    ui.keys(Keys.ESCAPE)
    ui.wait(lambda: not ui.viewer_open())

    # Shift+click a file of another folder: side A, and that folder becomes the current one
    ui.d.get(ui.url + "#file=a.svg&vs=b.svg")
    ui.wait(lambda: ui.visible_panes() == 2 and files_in(ui, "sub"))
    ActionChains(ui.d).key_down(Keys.SHIFT).click(side_row(ui, "mm.svg")).key_up(Keys.SHIFT).perform()
    ui.wait(lambda: ui.by_id("v-name").text == "mm.svg ⇄ /b.svg")
    assert "dir=sub" in ui.hash() and "vsdir=&" in ui.hash() + "&"
    assert ui.card_names() == ["mm.svg", "zipped.svgz"]


def test_compare_deep_link_across_folders(ui):
    ui.d.get(ui.url + "#dir=single&file=only.svg&vs=mm.svg&vsdir=sub")
    ui.wait(lambda: ui.by_id("v-name").text == "only.svg ⇄ /sub/mm.svg")
    assert ui.visible_panes() == 2


def test_sidebar_opens_files_and_follows_the_viewer(ui):
    ui.wait(lambda: side_files(ui))
    side_row(ui, "b.svg").click()
    ui.wait(lambda: ui.viewer_open() and ui.by_id("v-name").text == "b.svg")
    assert [n for n, _, cur, _ in side_files(ui) if cur] == ["b.svg"]
    side_row(ui, "a.svg").click()  # switches the file, no extra history entry
    ui.wait(lambda: ui.by_id("v-name").text == "a.svg")
    ui.keys(Keys.ARROW_RIGHT)
    ui.wait(lambda: [n for n, _, cur, _ in side_files(ui) if cur] == ["b.svg"])
    ui.keys(Keys.ESCAPE)
    ui.wait(lambda: not ui.viewer_open())
    assert not [n for n, _, cur, _ in side_files(ui) if cur]


def test_sidebar_in_compare_mode(ui):
    ui.d.get(ui.url + "#file=a.svg&vs=b.svg")
    ui.wait(lambda: ui.visible_panes() == 2 and side_files(ui))
    marks = lambda: {n: ab for n, _, _, ab in side_files(ui) if ab}
    ui.wait(lambda: marks() == {"a.svg": "A", "b.svg": "B"})
    side_row(ui, "viewbox-only.svg").click()  # click: side B
    ui.wait(lambda: ui.by_id("v-name").text == "a.svg ⇄ viewbox-only.svg")
    ActionChains(ui.d).key_down(Keys.SHIFT).click(side_row(ui, "viewbox-only.svg")).key_up(Keys.SHIFT).perform()
    ui.wait(lambda: marks() == {"viewbox-only.svg": "A B"})  # Shift+click: side A


def test_sidebar_thumbnails_and_hover_text(ui):
    ui.wait(lambda: ui.js("return [...document.querySelectorAll('.card img')].every(i => i.getAttribute('src'))"),
            timeout=60)
    ui.wait(lambda: ui.js("return [...document.querySelectorAll('#tree .row.file .mini')].every(i => i.getAttribute('src'))"))
    row = side_row(ui, "a.svg")
    ActionChains(ui.d).move_to_element(row).perform()
    ui.wait(lambda: row.get_attribute("title") == "a.svg\n200×100 · " + ui.js(
        "return document.querySelector('.card .meta .size').textContent"))


def test_sidebar_can_be_resized_and_keeps_its_width(ui):
    side = ui.by_id("side")
    before = side.rect["width"]
    grip = ui.by_id("side-grip")
    # the drag starts at the middle of the handle, so the new width is where the pointer ends up
    wanted = grip.rect["x"] + grip.rect["width"] / 2 + 120 - side.rect["x"]
    ActionChains(ui.d).move_to_element(grip).click_and_hold().move_by_offset(60, 0) \
        .move_by_offset(60, 0).release().perform()
    ui.wait(lambda: abs(side.rect["width"] - wanted) <= 2)
    ui.d.get("about:blank")
    ui.d.get(ui.url)
    ui.wait(lambda: abs(ui.by_id("side").rect["width"] - wanted) <= 2)
    ActionChains(ui.d).double_click(ui.by_id("side-grip")).perform()  # back to the default
    ui.wait(lambda: abs(ui.by_id("side").rect["width"] - before) <= 2)


def test_sidebar_files_dont_wait_for_parent_folders(ui, monkeypatch):
    # On a slow (network) drive, listing a parent folder is slow: the server reads the start of
    # every file in it. The current folder's files must show without waiting for that.
    real = esvigy.read_head

    def slow_read_head(path):
        if path.parent == ui.root:
            time.sleep(1.0)
        return real(path)

    monkeypatch.setattr(esvigy, "read_head", slow_read_head)
    ui.d.get("about:blank")
    t0 = time.time()
    ui.d.get(ui.url + "#dir=sub")  # a fresh page: nothing listed yet
    ui.wait(lambda: [n for n, *_ in side_files(ui)] == ["mm.svg", "zipped.svgz"], timeout=30)
    assert time.time() - t0 < 2.5, "the sidebar waited for the parent folder's listing"
    ui.wait(lambda: [r.text for r in ui.d.find_elements(By.CSS_SELECTOR, "#tree > ul > li > ul > li > .row .label")]
            [:1] == ["docs"], timeout=30)  # the parent's subfolders still show up


def test_current_folder_can_be_folded(ui):
    ui.d.get(ui.url + "#dir=single")  # only files, no subfolders: still foldable
    ui.wait(lambda: files_in(ui, "single") == ["only.svg"])
    node = tree_node(ui, "single")
    assert node.find_element(By.CSS_SELECTOR, ".twisty").value_of_css_property("visibility") == "visible"
    visible = lambda: side_row(ui, "only.svg").is_displayed()
    node.click()  # the current folder: folds
    ui.wait(lambda: not visible())
    assert "dir=single" in ui.hash()  # still in that folder
    node.click()
    ui.wait(visible)
    node.find_element(By.CSS_SELECTOR, ".twisty").click()  # the arrow works too
    ui.wait(lambda: not visible())



# ---------------------------------------------------------------- files that change while shown

def rewrite(path, colour):
    """Change an SVG the way a plotting script does: new content, new modification time."""
    old = path.stat().st_mtime_ns
    path.write_text(SMALL.format(w=200, h=100, c=colour) + "<!-- %s -->" % colour)
    os.utime(path, ns=(old + 2_000_000_000, old + 2_000_000_000))


def test_changed_file_gets_a_new_thumbnail_while_shown(ui):
    ui.d.get(ui.url + "#dir=live")
    ui.wait(lambda: ui.card_names() == ["other.svg", "plot.svg"])
    thumb = lambda name: ui.js("""
        const c = [...document.querySelectorAll('.card')].find(c => c.querySelector('.name').textContent === arguments[0]);
        return c.querySelector('img').getAttribute('src');""", name)
    ui.wait(lambda: thumb("plot.svg") and thumb("other.svg"))
    before, other = thumb("plot.svg"), thumb("other.svg")
    ui.js("document.getElementById('grid').scrollTop = 0; window.__marker = 1")  # no page reload allowed
    rewrite(ui.root / "live" / "plot.svg", "#%06x" % (int(time.time() * 1000) & 0xFFFFFF))
    ui.wait(lambda: thumb("plot.svg") not in (None, before), timeout=15)
    assert thumb("other.svg") == other  # unchanged files keep theirs
    assert ui.js("return window.__marker") == 1


def test_changed_file_reloads_in_the_viewer(ui):
    ui.d.get(ui.url + "#dir=live&file=plot.svg")
    ui.wait(lambda: ui.by_id("v-time").text.endswith("ms"))
    src = lambda: ui.js("return document.querySelector('.pane:not([hidden]) img.img').getAttribute('src')")
    before = src()
    rewrite(ui.root / "live" / "plot.svg", "#%06x" % (int(time.time() * 1000 + 7) & 0xFFFFFF))
    ui.wait(lambda: src() not in (None, before), timeout=15)
    ui.wait(lambda: ui.by_id("v-time").text.endswith("ms"))
    assert ui.by_id("v-name").text == "plot.svg"


def test_shift_click_in_the_sidebar_selects_no_text(ui):
    ui.d.get(ui.url + "#file=a.svg&vs=b.svg")
    ui.wait(lambda: ui.visible_panes() == 2 and side_files(ui))
    ui.js("getSelection().removeAllRanges()")
    side_row(ui, "a.svg").click()
    ActionChains(ui.d).key_down(Keys.SHIFT).click(side_row(ui, "viewbox-only.svg")).key_up(Keys.SHIFT).perform()
    ui.wait(lambda: ui.by_id("v-name").text.startswith("viewbox-only.svg"))  # Shift+click still works
    assert ui.js("return getSelection().toString()") == ""
