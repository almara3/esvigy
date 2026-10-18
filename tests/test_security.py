"""Security checks: one test (or group) per attack the server has to withstand.

Threat model: the server listens on 127.0.0.1, so the attackers are other local users
or processes (who can connect to any port), web pages open in the same browser (which
can make requests to 127.0.0.1 but not read the answers), and the SVG files themselves.
"""

import os
import socket
import shutil
import stat
import subprocess
import sys
import threading
import time

import pytest

import esvigy
from conftest import make_pdf

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


@pytest.mark.parametrize("name", ["evil.pdf", "evil.ai"])
def test_raw_pdfs_download_unless_firefox_app(server, name):
    # Other browsers' PDF viewers can't be told not to run PDF scripts, and ignore the CSP.
    make_pdf(server.root / name, [(595, 842, (1, 1, 1))], js=b"app.alert('pwned');")
    r = server.request("GET", "/file/" + name)
    assert r.status == 200 and r.headers["Content-Disposition"] == "attachment; filename*=UTF-8''" + name
    server.srv.pdf_inline = True  # the Firefox app window, with pdfjs.enableScripting off
    assert "Content-Disposition" not in server.request("GET", "/file/" + name).headers


def test_svgs_are_never_downloads(server):
    assert "Content-Disposition" not in server.request("GET", "/file/a.svg").headers


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_chromium_profile_is_private():
    profile = esvigy.chromium_profile()
    try:
        assert stat.S_IMODE(os.stat(profile).st_mode) == 0o700
    finally:
        os.rmdir(profile)

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
          "kdialog", "kdialog.exe", "powershell.exe", "powershell.bat", "osascript",
          "google-chrome", "chrome.exe", "chrome.bat", "msedge.exe", "msedge.cmd", "chromium")
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(esvigy.sys, "platform", platform)
    monkeypatch.setattr(esvigy.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("SystemRoot", str(tmp_path / "Windows"))  # no real powershell there
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", "Downloads", "bin"]))  # relative entries only
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        monkeypatch.setenv(var, ".")
    assert esvigy.find_firefox() is None
    assert esvigy.find_chromium() is None
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


def test_thumb_upload_survives_locked_target(server, monkeypatch):
    # Windows refuses to replace a file that is open; the upload must not fail or leave temp files.
    def locked(self, target):
        raise PermissionError("in use")

    monkeypatch.setattr(esvigy.Path, "replace", locked)
    assert server.request("PUT", f"/thumb/{KEY}.webp", body=WEBP, headers=PUT_HEADERS).status == 204
    assert not [p for p in server.cache.iterdir() if p.name.startswith(".")]


# ---------------------------------------------------------------- the token stays off command lines
# Other users can read every process's command line (ps, /proc/<pid>/cmdline), so the browser is
# started with the path of a private page that forwards it, never with the secret URL itself.

@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setattr(esvigy, "DATA_DIR", d)
    monkeypatch.setattr(esvigy, "PROFILE_DIR", d / "firefox-profile")
    return d


def test_launch_page_is_private_and_forwards(data_dir):
    import html
    import re
    url = "http://127.0.0.1:1234/SECRETtoken/#file=a%20b.svg&vs=x\"y.svg"
    page = esvigy.launch_page(url)
    assert "SECRET" not in str(page)
    assert stat.S_IMODE(page.stat().st_mode) == 0o600
    if sys.platform != "win32":
        assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    target = re.search(r'http-equiv="refresh" content="0; url=([^"]*)"', page.read_text()).group(1)
    assert html.unescape(target) == url  # quotes and & survive, nothing breaks out of the attribute


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_launch_page_never_writes_through_an_existing_file(data_dir, tmp_path, monkeypatch):
    data_dir.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("keep")
    monkeypatch.setattr(esvigy.secrets, "token_hex", lambda n: "fixed")
    (data_dir / "open-fixed.html").symlink_to(victim)
    with pytest.raises(FileExistsError):
        esvigy.launch_page("http://127.0.0.1:1/t/")
    assert victim.read_text() == "keep"


def test_launch_page_is_deleted_once_the_browser_arrives(server, data_dir):
    page = server.srv.launch_page = esvigy.launch_page(server.url)
    assert server.request("GET", "/", auth=False).status == 403  # without the token: stays
    assert page.exists()
    assert server.request("GET", "/").status == 200
    assert not page.exists() and server.srv.launch_page is None


@pytest.mark.parametrize("browser, firefox, chromium", [
    ("auto", "/fake/firefox", "/fake/chrome"), ("app", "/fake/firefox", None), ("firefox", "/fake/firefox", None),
    ("auto", None, "/fake/chrome"), ("chromium", "/fake/firefox", "/fake/chrome"),  # Chromium app windows
    ("auto", None, None), ("default", None, None), ("none", None, None)])
def test_token_is_never_on_a_command_line(tree, cache, data_dir, monkeypatch, capsys, browser, firefox, chromium):
    started, servers = [], []

    class FakeProc:
        pid = -1  # no real process behind it

        def __init__(self, args, **kw):
            started.append(list(args))

        def wait(self, *a):
            return 0

        def poll(self):
            return 0

    real_make_server = esvigy.make_server

    def make_server(*a, **k):
        srv = real_make_server(*a, **k)
        srv.serve_forever = lambda: None  # return at once instead of serving
        servers.append(srv)
        return srv

    monkeypatch.setattr(esvigy, "make_server", make_server)
    monkeypatch.setattr(esvigy, "find_firefox", lambda: firefox)
    monkeypatch.setattr(esvigy, "find_chromium", lambda: chromium)
    monkeypatch.setattr(esvigy.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(esvigy.webbrowser, "open", lambda url: started.append([url]))
    monkeypatch.setattr(esvigy.sys, "argv", ["esvigy", str(tree / "a.svg"), "-b", browser])
    esvigy.main()
    servers[0].server_close()

    token = servers[0].token
    assert token in capsys.readouterr().out  # the terminal still shows the URL, for pasting
    assert all(token not in arg for args in started for arg in args), started
    if browser == "none":
        assert started == []
    else:
        assert len(started) == 1 and started[0][-1].split("=", 1)[-1].startswith("file:")  # also --app=file:...
    assert not list(data_dir.glob("open-*.html"))  # removed when esvigy quits


# ---------------------------------------------------------------- Chromium profile cleanup
# Chromium's helper processes (network service, zygote) outlive its main process for a moment
# and keep writing into the profile, which held copies of viewed files in its cache.

# A stand-in browser: the main process exits at once; a child it started keeps writing
# into the profile for a while, as Chromium's network service does.
FAKE_BROWSER = r"""
import os, subprocess, sys, time
profile, done = sys.argv[1], sys.argv[2]
if len(sys.argv) == 3:
    subprocess.Popen([sys.executable, __file__, profile, done, "child"])
    sys.exit(0)
os.makedirs(os.path.join(profile, "Default", "Cache"), exist_ok=True)
end = time.time() + 1.0
i = 0
while time.time() < end:
    try:
        with open(os.path.join(profile, "Default", "Cache", "data_%d" % i), "w") as f:
            f.write("cached file contents")
    except OSError:
        pass  # the folder is being deleted under us
    i += 1
    time.sleep(0.005)
open(done, "w").close()
"""


@pytest.mark.skipif(sys.platform == "win32", reason="process groups")
def test_chromium_profile_is_removed_after_its_helpers_exit(tmp_path):
    script = tmp_path / "fake_browser.py"
    script.write_text(FAKE_BROWSER)
    profile, done = esvigy.chromium_profile(), tmp_path / "helper-done"
    proc = subprocess.Popen([sys.executable, str(script), profile, str(done)], start_new_session=True)
    proc.wait()  # the "window" is closed; its helper is still busy
    esvigy.remove_profile(profile, proc)
    deadline = time.time() + 10
    while not done.exists() and time.time() < deadline:  # only now can nothing write any more
        time.sleep(0.05)
    try:
        assert done.exists()
        assert not os.path.exists(profile)
    finally:
        shutil.rmtree(profile, ignore_errors=True)


@pytest.mark.skipif(sys.platform == "win32", reason="process groups")
def test_chromium_app_window_gets_its_own_process_group(tmp_path):
    exe = tmp_path / "chromium"
    exe.write_text("#!/bin/sh\nsleep 30\n")
    exe.chmod(0o755)
    proc, profile = esvigy.open_browser("file:///nothing.html", "chromium-app", str(exe))
    try:
        assert os.getpgid(proc.pid) == proc.pid  # so remove_profile() can wait for all of it
    finally:
        proc.kill()
        esvigy.remove_profile(profile, proc)
    assert not os.path.exists(profile)
