"""Browser tests: drive the real UI in headless Firefox.

Skipped unless selenium is installed and Firefox is on PATH. Run only these with
`pytest -m e2e`, or everything else with `pytest -m "not e2e"`.
"""

import shutil
import threading

import pytest

import esvigy
from conftest import make_tree

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
    old_cache, esvigy.CACHE_DIR = esvigy.CACHE_DIR, base / "cache"
    srv = esvigy.make_server(root)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    opts = webdriver.FirefoxOptions()
    opts.add_argument("--headless")
    opts.add_argument("--width=1300")
    opts.add_argument("--height=800")
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
