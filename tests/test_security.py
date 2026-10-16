"""Security checks: one test (or group) per attack the server has to withstand.

Threat model: the server listens on 127.0.0.1, so the attackers are other local users
or processes (who can connect to any port), web pages open in the same browser (which
can make requests to 127.0.0.1 but not read the answers), and the SVG files themselves.
"""

import os
import socket
import stat
import sys
import threading

import pytest

import esvigy

KEY = "0123456789abcdef01234567"
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 fake"
PUT_HEADERS = {"X-Esvigy": "1"}

ENDPOINTS = [
    ("GET", "/"),
    ("GET", "/static/app.js"),
    ("GET", "/static/render.html"),
    ("GET", "/api/list"),
    ("GET", "/api/list?dir=sub"),
    ("GET", "/file/a.svg"),
    ("HEAD", "/file/a.svg"),
    ("GET", f"/thumb/{KEY}.webp"),
    ("POST", "/api/open"),
    ("PUT", f"/thumb/{KEY}.webp"),
]


def alive(server):
    return server.request("GET", "/api/list").status == 200


# ---------------------------------------------------------------- secret URL token
# Without it, anyone who finds the port could browse the folder, pop up the Open
# dialog on the user's desktop, or use <img> tags on a web page to probe for files.

@pytest.mark.parametrize("method, path", ENDPOINTS)
def test_every_endpoint_needs_the_token(server, method, path):
    body = WEBP if method == "PUT" else None
    r = server.request(method, path, body=body, headers=PUT_HEADERS, auth=False)
    assert r.status == 403
    assert server.srv.token.encode() not in r.body
    assert not server.cache.exists()


@pytest.mark.parametrize("mangle", [
    lambda t: "wrong",
    lambda t: "",
    lambda t: t[:-1],             # prefix of the real token
    lambda t: t + "x",            # real token plus more
    lambda t: t + "%2F",          # an encoded slash must not end the token early
    lambda t: t.replace(t[0], "%%%02X" % ord(t[0]), 1),  # percent-encoded token
])
def test_wrong_token_is_refused(server, mangle):
    bad = mangle(server.srv.token)
    for method, path in ENDPOINTS:
        body = WEBP if method == "PUT" else None
        assert server.request(method, f"/{bad}{path}", body=body, headers=PUT_HEADERS, auth=False).status == 403
    assert not server.cache.exists()


def test_token_is_long_random_and_per_server(tree, cache):
    a, b = esvigy.make_server(tree), esvigy.make_server(tree)
    try:
        assert a.token != b.token
        assert len(a.token) >= 32
        assert a.url.endswith(f"/{a.token}/")
    finally:
        a.server_close()
        b.server_close()


def test_token_without_slash_redirects(server):
    r = server.request("GET", "", auth=True)
    assert r.status == 301
    assert r.headers["Location"] == f"/{server.srv.token}/"


def test_token_not_leaked_to_other_sites(server):
    for path in ("/", "/static/render.html", "/file/a.svg"):
        assert server.request("GET", path).headers["Referrer-Policy"] == "no-referrer"


# ---------------------------------------------------------------- cross-site writes (CSRF)
# A web page can send a POST/PUT with a simple body, but not with a custom header.

def test_open_without_custom_header_is_refused(server, monkeypatch):
    monkeypatch.setattr(esvigy.subprocess, "run", lambda *a, **k: pytest.fail("dialog was started"))
    assert server.request("POST", "/api/open").status == 403


def test_thumb_put_without_custom_header_is_refused(server):
    assert server.request("PUT", f"/thumb/{KEY}.webp", body=WEBP).status == 403
    assert not server.cache.exists()


def test_cors_preflight_is_never_granted(server):
    r = server.request("OPTIONS", "/api/open", headers={"Origin": "http://evil.example",
                                                        "Access-Control-Request-Method": "POST",
                                                        "Access-Control-Request-Headers": "x-esvigy"})
    assert r.status >= 400
    assert "Access-Control-Allow-Origin" not in r.headers


@pytest.mark.parametrize("method, path", ENDPOINTS)
def test_no_cors_headers_anywhere(server, method, path):
    r = server.request(method, path, headers={"Origin": "http://evil.example"})
    assert not any(h.lower().startswith("access-control-") for h in r.headers)


# ---------------------------------------------------------------- DNS rebinding

@pytest.mark.parametrize("host", ["evil.example:{port}", "127.0.0.1", "127.0.0.1:1", "localhost.evil.example:{port}", ""])
def test_foreign_host_header_is_refused_even_with_token(server, host):
    assert server.request("GET", "/api/list", headers={"Host": host.format(port=server.port)}).status == 403


# ---------------------------------------------------------------- thumbnail cache poisoning

@pytest.mark.parametrize("body", [b"<html><script>alert(1)</script></html>", b"<svg onload=alert(1)/>",
                                  b"RIFF\x00\x00\x00\x00PNG!", b"\x89PNG\r\n\x1a\n" + b"x" * 20])
def test_thumb_put_only_accepts_webp(server, body):
    assert server.request("PUT", f"/thumb/{KEY}.webp", body=body, headers=PUT_HEADERS).status == 400
    assert not (server.cache / f"{KEY}.webp").exists()


def test_thumbs_are_never_sniffed(server):
    server.request("PUT", f"/thumb/{KEY}.webp", body=WEBP, headers=PUT_HEADERS)
    r = server.request("GET", f"/thumb/{KEY}.webp")
    assert r.headers["Content-Type"] == "image/webp"
    assert r.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_cache_dir_is_private(server):
    server.request("PUT", f"/thumb/{KEY}.webp", body=WEBP, headers=PUT_HEADERS)
    assert stat.S_IMODE(server.cache.stat().st_mode) == 0o700


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_firefox_profile_is_private(tmp_path, monkeypatch):
    monkeypatch.setattr(esvigy, "PROFILE_DIR", tmp_path / "profile")
    esvigy.prepare_profile()
    assert stat.S_IMODE((tmp_path / "profile").stat().st_mode) == 0o700



def test_app_profile_disables_pdf_scripting(tmp_path, monkeypatch):
    # "raw" opens PDFs in Firefox's own viewer, which would run their JavaScript.
    monkeypatch.setattr(esvigy, "PROFILE_DIR", tmp_path / "profile")
    esvigy.prepare_profile()
    assert 'user_pref("pdfjs.enableScripting", false);' in (tmp_path / "profile" / "user.js").read_text()

# ---------------------------------------------------------------- malformed requests
# Each of these used to raise inside the handler instead of answering.

@pytest.mark.parametrize("path", ["/file/a%00.svg", "/file/%00", "/api/list?dir=sub%00", "/static/app%00.js",
                                  "/thumb/%00.webp"])
def test_nul_bytes_are_rejected_cleanly(server, path):
    assert server.request("GET", path).status == 404
    assert alive(server)


@pytest.mark.parametrize("length", ["abc", "-5", "1e3", " "])
def test_bad_content_length_is_rejected_cleanly(server, length):
    with socket.create_connection(("127.0.0.1", server.port), timeout=5) as s:
        s.sendall(f"PUT /{server.srv.token}/thumb/{KEY}.webp HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\n"
                  f"X-Esvigy: 1\r\nContent-Length: {length}\r\n\r\n".encode())
        assert s.recv(100).startswith(b"HTTP/1.1 400")
    assert alive(server)


def test_short_body_is_rejected(server):
    with socket.create_connection(("127.0.0.1", server.port), timeout=5) as s:
        s.sendall(f"PUT /{server.srv.token}/thumb/{KEY}.webp HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\n"
                  f"X-Esvigy: 1\r\nContent-Length: 100\r\n\r\n".encode() + WEBP)
        s.shutdown(socket.SHUT_WR)
        assert s.recv(100).startswith(b"HTTP/1.1 400")
    assert not (server.cache / f"{KEY}.webp").exists()


# ---------------------------------------------------------------- staying inside the root

@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_symlinks_out_of_root_are_not_listed(server, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.svg").write_text('<svg width="7" height="7"/>')
    (server.root / "link.svg").symlink_to(outside / "secret.svg")
    (server.root / "linkdir").symlink_to(outside)
    d = server.get_json("/api/list")
    assert "link.svg" not in [f["name"] for f in d["files"]]
    assert "linkdir" not in d["dirs"]
    assert server.request("GET", "/api/list?dir=linkdir").status == 404


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_symlinks_inside_root_still_work(server):
    (server.root / "alias.svg").symlink_to(server.root / "a.svg")
    assert "alias.svg" in [f["name"] for f in server.get_json("/api/list")["files"]]
    assert server.request("GET", "/file/alias.svg").status == 200


def test_static_dir_cannot_be_escaped(server):
    for path in ("/static/../esvigy.py", "/static/..%2Fesvigy.py", "/static/%2e%2e%2fesvigy.py", "/static/",
                 "/static//etc/passwd"):
        assert server.request("GET", path).status == 404


def test_listing_survives_root_switch_mid_request(server, tmp_path, monkeypatch):
    # /api/open can change the root while another thread is listing; that request must
    # finish against the root it started with instead of crashing.
    other = tmp_path / "other"
    other.mkdir()
    orig = esvigy.Handler.resolve

    def resolve_then_switch(root, rel):
        p = orig(root, rel)
        server.srv.root = other.resolve()
        return p

    monkeypatch.setattr(esvigy.Handler, "resolve", staticmethod(resolve_then_switch))
    d = server.get_json("/api/list?dir=sub")
    assert d["dir"] == "sub" and "mm.svg" in [f["name"] for f in d["files"]]


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions, non-root")
def test_errors_do_not_leak_absolute_paths(server):
    locked = server.root / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        r = server.request("GET", "/api/list?dir=locked")
        assert r.status == 403
        assert str(server.root).encode() not in r.body
    finally:
        locked.chmod(0o700)


# ---------------------------------------------------------------- browser-side hardening

def test_svg_cannot_run_scripts_when_opened_raw(server):
    for path in ("/file/a.svg", "/file/sub/zipped.svgz", "/file/viewbox-only.svg?fix"):
        r = server.request("GET", path)
        csp = r.headers["Content-Security-Policy"]
        assert "default-src 'none'" in csp and "sandbox" in csp
        assert "script-src" not in csp
        assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_ui_cannot_be_framed_and_allows_no_inline_scripts(server):
    csp = server.request("GET", "/").headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "default-src 'self'" in csp


def test_renderer_can_only_be_framed_by_the_ui(server):
    csp = server.request("GET", "/static/render.html").headers["Content-Security-Policy"]
    ancestors = csp.split("frame-ancestors ")[1].split(";")[0].split()
    assert sorted(ancestors) == sorted([f"http://127.0.0.1:{server.port}", f"http://localhost:{server.port}"])
    assert "unsafe-inline" not in csp


def test_static_html_has_no_inline_script_or_handlers():
    # The CSP forbids them; this catches them before a browser silently ignores them.
    import re
    for page in esvigy.STATIC_DIR.glob("*.html"):
        html = page.read_text()
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), page.name
        assert not re.search(r"\son[a-z]+\s*=", html), page.name


@pytest.mark.parametrize("method, path", ENDPOINTS)
def test_nosniff_everywhere(server, method, path):
    r = server.request(method, path, body=WEBP if method == "PUT" else None)
    assert r.headers.get("X-Content-Type-Options") == "nosniff"


# ---------------------------------------------------------------- server stays responsive

def test_open_dialog_does_not_block_other_requests(server, tmp_path, monkeypatch):
    release = threading.Event()

    def slow_dialog(*a, **k):
        release.wait(5)
        raise OSError("cancelled")

    monkeypatch.setattr(esvigy.sys, "platform", "linux")
    monkeypatch.setattr(esvigy, "find_program", lambda name: "/bin/true" if name == "zenity" else None)
    monkeypatch.setattr(esvigy.subprocess, "run", slow_dialog)
    t = threading.Thread(target=server.request, args=("POST", "/api/open"), kwargs={"headers": PUT_HEADERS})
    t.start()
    try:
        assert alive(server)
    finally:
        release.set()
        t.join()


# ---------------------------------------------------------------- programs we start (binary planting)
# On Windows, shutil.which() and subprocess look in the current directory first and accept
# .bat/.cmd. A planted firefox.bat in the folder esvigy is started from must never run.

def plant(folder, *names):
    folder.mkdir(parents=True, exist_ok=True)
    for n in names:
        (folder / n).write_text("@echo pwned\n")
        (folder / n).chmod(0o755)


@pytest.mark.parametrize("platform", ["linux", "win32", "darwin"])
def test_programs_are_never_taken_from_the_current_directory(tmp_path, monkeypatch, platform):
    cwd = tmp_path / "Downloads"
    plant(cwd, "firefox", "firefox.exe", "firefox.bat", "firefox.cmd", "zenity", "zenity.exe", "zenity.bat",
          "kdialog", "kdialog.exe", "powershell.exe", "powershell.bat", "osascript")
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(esvigy.sys, "platform", platform)
    monkeypatch.setattr(esvigy.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("SystemRoot", str(tmp_path / "Windows"))  # no real powershell there
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", "Downloads", "bin"]))  # relative entries only
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        monkeypatch.setenv(var, ".")
    assert esvigy.find_firefox() is None
    assert esvigy.find_program("zenity") is None
    cmd = esvigy.folder_dialog_cmd(tmp_path)
    if platform != "darwin":  # macOS uses the fixed /usr/bin/osascript
        assert cmd is None
    else:
        assert cmd is None or cmd[0] == "/usr/bin/osascript"


def test_windows_only_runs_exe_files(tmp_path, monkeypatch):
    plant(tmp_path / "bin", "firefox.bat", "firefox.cmd", "firefox")
    monkeypatch.setattr(esvigy.sys, "platform", "win32")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert esvigy.find_program("firefox") is None
    plant(tmp_path / "bin", "firefox.exe")
    assert esvigy.find_program("firefox") == str(tmp_path / "bin" / "firefox.exe")


def test_started_programs_are_full_paths(tmp_path, monkeypatch):
    plant(tmp_path / "bin", "zenity")
    monkeypatch.setattr(esvigy.sys, "platform", "linux")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert os.path.isabs(esvigy.folder_dialog_cmd("/x")[0])
    plant(tmp_path / "Windows" / "System32" / "WindowsPowerShell" / "v1.0", "powershell.exe")
    monkeypatch.setattr(esvigy.sys, "platform", "win32")
    monkeypatch.setenv("SystemRoot", str(tmp_path / "Windows"))
    assert esvigy.folder_dialog_cmd("/x")[0] == str(tmp_path / "Windows" / "System32" / "WindowsPowerShell"
                                                     / "v1.0" / "powershell.exe")


# ---------------------------------------------------------------- Windows paths

@pytest.mark.parametrize("rel", [r"\\evil.example\share\x.svg", r"..\..\x.svg", "C:/Windows/x.svg",
                                 "c:x.svg", "a.svg:stream", "sub\\mm.svg"])
def test_windows_paths_are_refused_before_touching_disk_or_network(tree, monkeypatch, rel):
    monkeypatch.setattr(esvigy.sys, "platform", "win32")
    monkeypatch.setattr(esvigy.Path, "resolve", lambda *a, **k: pytest.fail("resolve() was called"))
    assert esvigy.Handler.resolve(tree, rel) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file names")
def test_colons_and_backslashes_are_plain_names_elsewhere(server):
    (server.root / "a:b\\c.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>')
    assert "a:b\\c.svg" in [f["name"] for f in server.get_json("/api/list")["files"]]
    assert server.request("GET", "/file/a%3Ab%5Cc.svg").status == 200
