import gzip
import http.client
import json
import threading
from types import SimpleNamespace

import pytest

import esvigy

SMALL = '<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}"><rect width="{w}" height="{h}" fill="{c}"/></svg>'


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
