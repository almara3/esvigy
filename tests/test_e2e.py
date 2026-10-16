"""Browser tests: drive the real UI in headless Firefox.

Skipped unless selenium is installed and Firefox is on PATH. Run only these with
`pytest -m e2e`, or everything else with `pytest -m "not e2e"`.
"""

import json
import re
import shutil
import threading
import time

import pytest

import esvigy
from conftest import make_pdf, make_tree

webdriver = pytest.importorskip("selenium.webdriver")
from selenium.webdriver.common.action_chains import ActionChains  # noqa: E402
from selenium.webdriver.common.by import By  # noqa: E402
from selenium.webdriver.common.keys import Keys  # noqa: E402
from selenium.webdriver.support.ui import WebDriverWait  # noqa: E402

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not shutil.which("firefox"), reason="Firefox not installed"),
]


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    """One server and one browser for the whole module (starting Firefox is slow)."""
    base = tmp_path_factory.mktemp("e2e")
    root = make_tree(base / "root")
    (root / "docs").mkdir()
    make_pdf(root / "docs" / "a4.pdf", [(595, 842, (0, .5, .5))])
    make_pdf(root / "docs" / "two.pdf", [(842, 595, (.8, .1, .1)), (595, 842, (0, 0, 1))])
    make_pdf(root / "docs" / "three.pdf", [(595, 842, (0, .6, 0)), (595, 842, (1, .8, 0)), (595, 842, (.8, 0, .8))])
    # too much for one drawing slice: guards against thumbnails that never finish
    make_pdf(root / "docs" / "heavy.pdf", [(595, 842, (1, 1, 1))], shapes=150_000)
    old_cache, esvigy.CACHE_DIR = esvigy.CACHE_DIR, base / "cache"
    srv = esvigy.make_server(root)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    opts = webdriver.FirefoxOptions()
    opts.add_argument("--headless")
    opts.add_argument("--width=1300")
    opts.add_argument("--height=800")
    # Same preferences as esvigy's own app window, so security settings there are tested too.
    for name, value in re.findall(r'^user_pref\("([^"]+)", (.+)\);$', esvigy.USER_JS, re.M):
        opts.set_preference(name, json.loads(value))
    driver = webdriver.Firefox(options=opts)
    try:
        yield driver, srv.url, root, base / "cache"
    finally:
        driver.quit()
        srv.shutdown()
        srv.server_close()
        esvigy.CACHE_DIR = old_cache


@pytest.fixture
def ui(app):
    """A fresh page with default preferences."""
    driver, url, root, cache = app
    driver.get(url)
    driver.execute_script("localStorage.clear()")
    driver.get("about:blank")  # a real reload; driver.refresh() can hang here
    driver.get(url)
    ui = UI(driver, url, root, cache)
    ui.wait(lambda: ui.card_names())
    return ui


class UI:
    def __init__(self, driver, url, root, cache):
        self.d, self.url, self.root, self.cache = driver, url, root, cache

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
    # Firefox's own PDF viewer ignores our CSP; only the app profile's preference stops this.
    from selenium.common.exceptions import NoAlertPresentException
    (ui.root / "evil").mkdir(exist_ok=True)
    make_pdf(ui.root / "evil" / "js.pdf", [(595, 842, (1, 1, 1))], js=b"app.alert('pwned-pdf');")
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

    cap_button(0, 1).click()  # A: next, skipping the file B shows
    ui.wait(lambda: name() == "viewbox-only.svg ⇄ b.svg")
    cap_button(1, 0).click()  # B: previous
    ui.wait(lambda: name() == "viewbox-only.svg ⇄ a.svg")

    ui.by_id("v-next").click()  # top bar: both panes, wrapping around
    ui.wait(lambda: name() == "a.svg ⇄ b.svg")
    ui.keys(Keys.ARROW_DOWN)  # both
    ui.wait(lambda: name() == "b.svg ⇄ viewbox-only.svg")
    ui.keys(Keys.ARROW_UP)
    ui.wait(lambda: name() == "a.svg ⇄ b.svg")
    ui.keys(Keys.ARROW_RIGHT)  # unchanged: B only
    ui.wait(lambda: name() == "a.svg ⇄ viewbox-only.svg")


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
