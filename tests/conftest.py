import gzip
import http.client
import json
import threading
from types import SimpleNamespace

import pytest

import esvigy

SMALL = '<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}"><rect width="{w}" height="{h}" fill="{c}"/></svg>'


def make_pdf(path, pages, shapes=0, js=None):
    """Write a minimal valid PDF. `pages` is a list of (width_pt, height_pt, (r, g, b)).

    `shapes` adds that many small triangles to each page, to make it slow to draw.
    `js` (bytes, balanced parentheses) is JavaScript the PDF runs when opened.
    """
    action = b" /OpenAction << /S /JavaScript /JS (%s) >>" % js if js else b""
    objs = [b"<< /Type /Catalog /Pages 2 0 R%s >>" % action, None]
    kids = []
    for w, h, (r, g, b) in pages:
        # page filled with one colour, plus a black square in the top-left corner
        content = b"%.3f %.3f %.3f rg 0 0 %d %d re f 0 0 0 rg 10 %d 40 40 re f" % (r, g, b, w, h, h - 50)
        content += b"".join(b" %d %d m %d %d l %d %d l f" % (i % w, i * 7 % h, i % w + 9, i * 7 % h, i % w, i * 7 % h + 9)
                            for i in range(shapes))
        objs.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content))
        objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Contents %d 0 R >>"
                    % (w, h, len(objs)))
        kids.append(b"%d 0 R" % len(objs))
    objs[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(kids), len(kids))
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, o)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.write_bytes(bytes(out))
    return path


def make_tree(root):
    """A small folder of SVGs covering the cases the server treats specially."""
    (root / "sub" / "deeper").mkdir(parents=True)
    (root / "a.svg").write_text(SMALL.format(w=200, h=100, c="teal"))
    (root / "b.svg").write_text(SMALL.format(w=200, h=100, c="crimson"))
    (root / "viewbox-only.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50"><rect width="100" height="50"/></svg>')
    (root / "sub" / "mm.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="297mm"/>')
    (root / "sub" / "zipped.svgz").write_bytes(gzip.compress(SMALL.format(w=64, h=64, c="gold").encode()))
    (root / "notes.txt").write_text("not an svg")
    (root / ".hidden.svg").write_text(SMALL.format(w=1, h=1, c="red"))
    (root / ".hiddendir").mkdir()
    return root


@pytest.fixture
def tree(tmp_path):
    return make_tree(tmp_path / "root")


@pytest.fixture
def cache(tmp_path, monkeypatch):
    d = tmp_path / "cache"
    monkeypatch.setattr(esvigy, "CACHE_DIR", d)
    return d


@pytest.fixture
def server(tree, cache):
    """A running esvigy server over `tree`, with a helper to send raw requests.

    Paths are sent below the server's secret /<token> prefix unless `auth=False`.
    """
    srv = esvigy.make_server(tree)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    t.start()
    port = srv.server_port

    def request(method, path, body=None, headers=None, auth=True):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(method, ("/" + srv.token if auth else "") + path, body=body, headers=headers or {})
            r = conn.getresponse()
            return SimpleNamespace(status=r.status, headers=r.headers, body=r.read())
        finally:
            conn.close()

    def get_json(path):
        r = request("GET", path)
        assert r.status == 200, r.body
        return json.loads(r.body)

    yield SimpleNamespace(srv=srv, port=port, url=srv.url, root=tree, cache=cache,
                          request=request, get_json=get_json)
    srv.shutdown()
    srv.server_close()
