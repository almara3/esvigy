"""Tests against a running esvigy server (stdlib only, no browser)."""

import gzip
import json
import os
import stat
import sys
from urllib.parse import quote

import pytest

import esvigy

KEY = "0123456789abcdef01234567"
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 fake"
PUT_HEADERS = {"X-Esvigy": "1"}


# ---------------------------------------------------------------- pages and listing

def test_index_and_static(server):
    r = server.request("GET", "/")
    assert r.status == 200
    assert b'id="grid"' in r.body
    assert server.request("GET", "/static/app.js").headers["Content-Type"].startswith("text/javascript")
    assert server.request("GET", "/static/render.html").status == 200


@pytest.mark.parametrize("path", ["/static/../esvigy.py", "/static/%2e%2e/esvigy.py", "/static/nope.js", "/nope"])
def test_static_not_found(server, path):
    assert server.request("GET", path).status == 404


def test_list_root(server):
    d = server.get_json("/api/list")
    assert d["dir"] == "" and d["root"] == "root"
    assert d["dirs"] == ["sub"]  # hidden dirs skipped
    files = {f["name"]: f for f in d["files"]}
    assert set(files) == {"a.svg", "b.svg", "viewbox-only.svg"}  # no .txt, no hidden files
    a = files["a.svg"]
    assert (a["w"], a["h"], a["intrinsic"], a["thumb"]) == (200, 100, True, False)
    assert a["size"] == (server.root / "a.svg").stat().st_size
    assert len(a["key"]) == 24
    vb = files["viewbox-only.svg"]
    assert (vb["w"], vb["h"], vb["intrinsic"]) == (100, 50, False)


def test_list_subdir(server):
    d = server.get_json("/api/list?dir=sub")
    assert d["dir"] == "sub" and d["dirs"] == ["deeper"]
    files = {f["name"]: f for f in d["files"]}
    assert files["mm.svg"]["w"] == pytest.approx(793.701, abs=0.01)
    assert files["zipped.svgz"]["w"] == 64


@pytest.mark.parametrize("q", ["dir=..", "dir=../..", "dir=nope", "dir=a.svg", "dir=%2Fetc"])
def test_list_outside_or_missing(server, q):
    assert server.request("GET", "/api/list?" + q).status == 404


# ---------------------------------------------------------------- serving SVGs

def test_file_is_served_safely(server):
    r = server.request("GET", "/file/a.svg")
    assert r.status == 200
    assert r.body == (server.root / "a.svg").read_bytes()
    assert r.headers["Content-Type"] == "image/svg+xml"
    assert "default-src 'none'" in r.headers["Content-Security-Policy"]


def test_file_with_spaces_and_unicode(server):
    name = "my drawing ü #1.svg"
    (server.root / name).write_text('<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>')
    assert server.request("GET", "/file/" + quote(name)).status == 200


def test_svgz_is_sent_gzip_encoded(server):
    r = server.request("GET", "/file/sub/zipped.svgz")
    assert r.status == 200
    assert r.headers["Content-Encoding"] == "gzip"
    assert gzip.decompress(r.body).startswith(b"<svg")


@pytest.mark.parametrize("path", [
    "/file/notes.txt",
    "/file/missing.svg",
    "/file/sub",
    "/file/..%2F..%2Fetc%2Fpasswd",
])
def test_file_not_found(server, path):
    assert server.request("GET", path).status == 404


def test_dotdot_out_of_root_is_refused(server):
    (server.root.parent / "outside.svg").write_text("<svg/>")
    assert server.request("GET", "/file/../outside.svg").status == 404
    assert server.request("GET", "/file/%2E%2E%2Foutside.svg").status == 404


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_symlink_out_of_root_is_refused(server, tmp_path):
    outside = tmp_path / "secret.svg"
    outside.write_text("<svg/>")
    (server.root / "link.svg").symlink_to(outside)
    assert server.request("GET", "/file/link.svg").status == 404


def test_fix_adds_intrinsic_size(server):
    r = server.request("GET", "/file/viewbox-only.svg?fix")
    assert r.status == 200
    assert esvigy.svg_size(r.body) == (100, 50, True)
    assert int(r.headers["Content-Length"]) == len(r.body)


def test_fix_on_large_file_keeps_the_rest_intact(server):
    # Bigger than the header window, so the server splices patched head + original tail.
    body = b"".join(b'<circle cx="%d" cy="1" r="1"/>' % i for i in range(20000))
    src = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">' + body + b"</svg>"
    assert len(src) > esvigy.HEAD_BYTES
    (server.root / "big.svg").write_bytes(src)
    r = server.request("GET", "/file/big.svg?fix")
    assert int(r.headers["Content-Length"]) == len(r.body)
    assert esvigy.svg_size(r.body)[2] is True
    assert r.body.replace(b' width="10.000" height="10.000"', b"") == src


def test_fix_leaves_sized_files_alone(server):
    r = server.request("GET", "/file/a.svg?fix")
    assert r.body == (server.root / "a.svg").read_bytes()


def test_etag_revalidation(server):
    r = server.request("GET", "/file/a.svg")
    etag = r.headers["ETag"]
    assert server.request("GET", "/file/a.svg", headers={"If-None-Match": etag}).status == 304
    (server.root / "a.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="3" height="3"/>')
    assert server.request("GET", "/file/a.svg", headers={"If-None-Match": etag}).status == 200


def test_head_request_has_no_body(server):
    r = server.request("HEAD", "/file/a.svg")
    assert r.status == 200 and r.body == b""
    assert int(r.headers["Content-Length"]) == (server.root / "a.svg").stat().st_size


# ---------------------------------------------------------------- host check (DNS rebinding)

@pytest.mark.parametrize("method", ["GET", "PUT", "POST"])
def test_foreign_host_is_rejected(server, method):
    r = server.request(method, "/api/list", headers={"Host": "evil.example:%d" % server.port, "X-Esvigy": "1"})
    assert r.status == 403


def test_localhost_is_accepted(server):
    # The thumbnail renderer frame runs on localhost, the UI on 127.0.0.1.
    assert server.request("GET", "/api/list", headers={"Host": "localhost:%d" % server.port}).status == 200


# ---------------------------------------------------------------- thumbnail cache

def test_thumb_roundtrip(server):
    data = WEBP
    assert server.request("GET", f"/thumb/{KEY}.webp").status == 404
    assert server.request("PUT", f"/thumb/{KEY}.webp", body=data, headers=PUT_HEADERS).status == 204
    assert (server.cache / f"{KEY}.webp").read_bytes() == data
    r = server.request("GET", f"/thumb/{KEY}.webp")
    assert r.body == data
    assert r.headers["Content-Type"] == "image/webp"
    assert "immutable" in r.headers["Cache-Control"]


def test_listing_reports_cached_thumbs(server):
    key = next(f["key"] for f in server.get_json("/api/list")["files"] if f["name"] == "a.svg")
    server.request("PUT", f"/thumb/{key}.webp", body=WEBP, headers=PUT_HEADERS)
    a = next(f for f in server.get_json("/api/list")["files"] if f["name"] == "a.svg")
    assert a["thumb"] is True


@pytest.mark.parametrize("name", ["..%2Fevil.webp", "short.webp", KEY + ".png", KEY.upper() + ".webp"])
def test_thumb_put_rejects_bad_names(server, name):
    assert server.request("PUT", "/thumb/" + name, body=WEBP, headers=PUT_HEADERS).status == 400
    assert not server.cache.exists() or not any(server.cache.iterdir())


def test_thumb_put_rejects_empty_and_huge(server):
    assert server.request("PUT", f"/thumb/{KEY}.webp", body=b"", headers=PUT_HEADERS).status == 400
    too_big = str(esvigy.MAX_THUMB_BYTES + 1)
    r = server.request("PUT", f"/thumb/{KEY}.webp", body=b"x", headers={**PUT_HEADERS, "Content-Length": too_big})
    assert r.status == 400


def test_put_elsewhere_is_not_found(server):
    assert server.request("PUT", "/file/a.svg", body=b"x", headers=PUT_HEADERS).status == 404


# ---------------------------------------------------------------- open folder dialog

def fake_dialog(tmp_path, monkeypatch, script):
    """Put a fake `zenity` first on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / "zenity"
    exe.write_text("#!/bin/sh\n" + script)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


def test_open_needs_custom_header(server):
    assert server.request("POST", "/api/open").status == 403


def test_open_switches_root(server, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    (other / "z.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>')
    fake_dialog(tmp_path, monkeypatch, f"echo '{other}'\n")
    r = server.request("POST", "/api/open", headers={"X-Esvigy": "1"})
    assert r.status == 200 and json.loads(r.body) == {"root": "other"}
    assert [f["name"] for f in server.get_json("/api/list")["files"]] == ["z.svg"]


def test_open_cancelled_keeps_root(server, tmp_path, monkeypatch):
    fake_dialog(tmp_path, monkeypatch, "exit 1\n")
    assert server.request("POST", "/api/open", headers={"X-Esvigy": "1"}).status == 204
    assert server.srv.root == server.root.resolve()


def test_open_without_dialog_tool(server, monkeypatch):
    monkeypatch.setattr(esvigy.shutil, "which", lambda name: None)
    assert server.request("POST", "/api/open", headers={"X-Esvigy": "1"}).status == 501


def test_post_elsewhere_is_not_found(server):
    assert server.request("POST", "/api/list", headers={"X-Esvigy": "1"}).status == 404
