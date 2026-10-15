"""Server side of PDF support: listing, serving, and the metadata cache."""

import json

import pytest

import esvigy
from conftest import make_pdf

KEY = "0123456789abcdef01234567"


@pytest.fixture
def tree(tmp_path):
    """Overrides conftest's tree: a folder with PDFs next to an SVG."""
    root = tmp_path / "root"
    root.mkdir()
    make_pdf(root / "a4.pdf", [(595, 842, (0, .5, .5))])
    make_pdf(root / "two pages.PDF", [(842, 595, (1, 0, 0)), (595, 842, (0, 0, 1))])
    (root / "pic.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>')
    (root / "fake.pdf.txt").write_text("not listed")
    return root


def files(server):
    return {f["name"]: f for f in server.get_json("/api/list")["files"]}


def test_pdfs_are_listed(server):
    fs = files(server)
    assert set(fs) == {"a4.pdf", "two pages.PDF", "pic.svg"}
    a4 = fs["a4.pdf"]
    assert a4["kind"] == "pdf" and a4["intrinsic"] is True
    # unknown until a browser measured it: A4 placeholder, no page count
    assert (a4["w"], a4["h"]) == pytest.approx(esvigy.A4_PX)
    assert a4["pages"] is None
    assert fs["pic.svg"]["kind"] == "svg"


def test_pdf_is_served(server):
    r = server.request("GET", "/file/a4.pdf")
    assert r.status == 200
    assert r.headers["Content-Type"] == "application/pdf"
    assert r.headers["Content-Security-Policy"] == esvigy.PDF_CSP
    assert r.body == (server.root / "a4.pdf").read_bytes()


def test_fix_is_ignored_for_pdfs(server):
    r = server.request("GET", "/file/a4.pdf?fix")
    assert r.body == (server.root / "a4.pdf").read_bytes()


def test_vendored_pdfjs_is_served(server):
    js = server.request("GET", "/static/vendor/pdfjs/pdf.min.mjs")
    assert js.status == 200 and js.headers["Content-Type"].startswith("text/javascript")
    assert server.request("GET", "/static/vendor/pdfjs/pdf.worker.min.mjs").status == 200
    wasm = server.request("GET", "/static/vendor/pdfjs/wasm/openjpeg.wasm")
    assert wasm.headers["Content-Type"] == "application/wasm"
    # PDF scripting engine is deliberately not shipped
    assert server.request("GET", "/static/vendor/pdfjs/wasm/quickjs-eval.wasm").status == 404


def put_meta(server, body, key=KEY):
    return server.request("PUT", f"/thumb/{key}.json", body=body, headers={"X-Esvigy": "1"})


def test_metadata_roundtrip_through_listing(server):
    key = files(server)["two pages.PDF"]["key"]
    assert put_meta(server, json.dumps({"w": 1122.67, "h": 793.7, "pages": 2}), key).status == 204
    f = files(server)["two pages.PDF"]
    assert (f["w"], f["h"], f["pages"]) == (1122.67, 793.7, 2)


def test_metadata_is_stored_canonical(server):
    body = json.dumps({"w": 1, "h": 2, "pages": 3, "extra": "<script>"})
    assert put_meta(server, body).status == 204
    assert json.loads((server.cache / f"{KEY}.json").read_text()) == {"w": 1, "h": 2, "pages": 3}


@pytest.mark.parametrize("body", [
    "not json",
    "[]",
    '{"w": 1, "h": 2}',
    '{"w": 0, "h": 2, "pages": 1}',
    '{"w": 1, "h": -2, "pages": 1}',
    '{"w": 1e9, "h": 2, "pages": 1}',
    '{"w": "NaN", "h": 2, "pages": 1}',
    '{"w": 1, "h": 2, "pages": 0}',
    '{"w": 1, "h": 2, "pages": 1.5}',
])
def test_bad_metadata_is_rejected(server, body):
    assert put_meta(server, body).status == 400
    assert not (server.cache / f"{KEY}.json").exists()


def test_metadata_size_limit(server):
    big = json.dumps({"w": 1, "h": 2, "pages": 3, "pad": "x" * esvigy.MAX_META_BYTES})
    assert put_meta(server, big).status == 400


def test_metadata_needs_header_and_is_not_downloadable(server):
    body = json.dumps({"w": 1, "h": 2, "pages": 3})
    assert server.request("PUT", f"/thumb/{KEY}.json", body=body).status == 403
    assert put_meta(server, body).status == 204
    assert server.request("GET", f"/thumb/{KEY}.json").status == 404


def test_corrupt_metadata_file_falls_back(server):
    key = files(server)["a4.pdf"]["key"]
    server.cache.mkdir(parents=True, exist_ok=True)
    (server.cache / f"{key}.json").write_text("garbage")
    f = files(server)["a4.pdf"]
    assert f["pages"] is None and (f["w"], f["h"]) == pytest.approx(esvigy.A4_PX)
