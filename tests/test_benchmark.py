"""The benchmark's helper server must expose only the files being measured."""

import http.client
import importlib.util
from pathlib import Path
from urllib.parse import quote

import pytest

spec = importlib.util.spec_from_file_location("bench", Path(__file__).resolve().parent.parent / "benchmark" / "bench.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'
ODD = "my fig #2 ?.svg"  # characters that break unescaped URLs


@pytest.fixture(scope="module")
def work(tmp_path_factory):
    """A folder like ~/work: one SVG to measure, plus things that must stay private."""
    tmp_path = tmp_path_factory.mktemp("work")
    (tmp_path / "fig.svg").write_bytes(SVG)
    (tmp_path / ODD).write_bytes(SVG)
    (tmp_path / "other.svg").write_bytes(SVG)
    (tmp_path / "secret.txt").write_text("private")
    (tmp_path / "index.html").write_text("<script>alert(1)</script>")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "fig.svg").write_bytes(SVG)
    return tmp_path


@pytest.fixture(scope="module")
def get(work):
    """One server for the module: shutting one down takes half a second."""
    srv = bench.serve(work, ["fig.svg", ODD])
    port = srv.server_port

    def get(path, host=f"127.0.0.1:{port}", method="GET"):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(method, path, headers={"Host": host})
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()

    yield get
    srv.shutdown()
    srv.server_close()


def test_serves_the_measured_files(get):
    assert get("/fig.svg") == (200, SVG)
    assert get("/fig.svg?0.123") == (200, SVG)  # the cache-busting query the engines add
    assert get("/" + quote(ODD)) == (200, SVG)


def test_root_is_a_blank_page_not_a_listing(get):
    status, body = get("/")
    assert status == 200
    assert b"fig.svg" not in body and b"secret" not in body and b"alert" not in body


@pytest.mark.parametrize("path", ["/other.svg", "/secret.txt", "/index.html", "/sub/", "/sub/fig.svg",
                                  "/../fig.svg", "/%2e%2e/fig.svg", "/fig.svg/", "//sub/fig.svg"])
def test_nothing_else_is_served(get, path):
    assert get(path)[0] == 404


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_foreign_host_is_refused(get, method):
    assert get("/fig.svg", host="evil.example", method=method)[0] == 403
