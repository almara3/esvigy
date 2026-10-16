"""Raster images (PNG, JPEG, …), TIFF and Illustrator files: measuring, listing, serving."""

import json
import struct

import pytest

import esvigy
from conftest import make_bmp, make_jpeg_header, make_pdf, make_png, make_tiff


# ---------------------------------------------------------------- measuring

HEADERS = {
    "gif": b"GIF89a" + struct.pack("<HH", 640, 480) + b"\0" * 20,
    "webp-lossy": b"RIFF\0\0\0\0WEBPVP8 \0\0\0\0" + b"\0\0\0\x9d\x01\x2a" + struct.pack("<HH", 640, 480) + b"\0" * 8,
    "webp-lossless": b"RIFF\0\0\0\0WEBPVP8L\0\0\0\0\x2f" + struct.pack("<I", (640 - 1) | (480 - 1) << 14) + b"\0" * 8,
    "webp-extended": b"RIFF\0\0\0\0WEBPVP8X\0\0\0\0\0\0\0\0" + (640 - 1).to_bytes(3, "little")
                     + (480 - 1).to_bytes(3, "little") + b"\0" * 8,
    "ico": b"\0\0\1\0\2\0" + bytes([16, 16]) + b"\0" * 14 + bytes([0, 0]) + b"\0" * 14,  # 16 px and 256 px
    "avif": b"\0\0\0\x1cftypavif" + b"\0" * 16 + b"\0\0\0\x14ispe\0\0\0\0" + struct.pack(">II", 640, 480),
    "jpeg": make_jpeg_header(640, 480),
}


@pytest.mark.parametrize("name", HEADERS)
def test_image_size_from_header(tmp_path, name):
    p = tmp_path / "img"
    p.write_bytes(HEADERS[name])
    assert esvigy.image_size(p) == ((256, 256) if name == "ico" else (640, 480))


def test_image_size_of_real_files(tmp_path):
    assert esvigy.image_size(make_png(tmp_path / "a.png", 30, 20, (1, 2, 3))) == (30, 20)
    assert esvigy.image_size(make_bmp(tmp_path / "a.bmp", 31, 20, (1, 2, 3))) == (31, 20)


@pytest.mark.parametrize("big_endian", [False, True])
def test_tiff_size_with_directory_at_the_end(tmp_path, big_endian):
    # The pixels come first, so the directory is beyond the bytes read for other formats.
    p = make_tiff(tmp_path / "a.tif", [(300, 250, (0, 0, 0))], big_endian=big_endian)
    assert p.stat().st_size > esvigy.HEAD_BYTES
    assert esvigy.image_size(p) == (300, 250)


@pytest.mark.parametrize("orientation, size", [(1, (640, 480)), (3, (640, 480)), (6, (480, 640)), (8, (480, 640))])
@pytest.mark.parametrize("big_endian", [False, True])
def test_jpeg_size_follows_exif_rotation(tmp_path, orientation, size, big_endian):
    p = tmp_path / "photo.jpg"
    p.write_bytes(make_jpeg_header(640, 480, orientation, big_endian))
    assert esvigy.image_size(p) == size


@pytest.mark.parametrize("data", [
    b"", b"\x89PNG\r\n\x1a\n", b"GIF8", b"\xff\xd8\xff", b"\xff\xd8\xff\xe1\xff\xff", b"BM", b"II*\0\xff\xff\xff\xff",
    b"MM\0*\0\0\0\x08\0\x01", b"RIFF\0\0\0\0WEBPVP8 ", b"\0\0\1\0\x05\0", b"\0\0\0\x1cftypavif", b"random bytes",
])
def test_broken_or_truncated_headers_give_none(tmp_path, data):
    p = tmp_path / "x"
    p.write_bytes(data)
    assert esvigy.image_size(p) is None


# ---------------------------------------------------------------- listing and serving

@pytest.fixture
def server(server):
    root = server.root / "img"
    root.mkdir()
    make_png(root / "a.png", 30, 20, (0, 128, 0))
    (root / "photo.JPG").write_bytes(make_jpeg_header(640, 480, orientation=6))
    make_bmp(root / "c.bmp", 31, 20, (0, 0, 255))
    make_tiff(root / "scan.tiff", [(40, 20, (255, 0, 0)), (20, 40, (0, 0, 255))])
    make_pdf(root / "logo.ai", [(595, 842, (1, 0, 0))])
    (root / "broken.gif").write_bytes(b"not a gif")
    return server


def listed(server):
    return {f["name"]: f for f in server.get_json("/api/list?dir=img")["files"]}


def test_images_tiffs_and_ai_are_listed(server):
    fs = listed(server)
    assert set(fs) == {"a.png", "photo.JPG", "c.bmp", "scan.tiff", "logo.ai", "broken.gif"}
    assert {n: f["kind"] for n, f in fs.items()} == {
        "a.png": "image", "photo.JPG": "image", "c.bmp": "image", "broken.gif": "image",
        "scan.tiff": "tiff", "logo.ai": "pdf"}
    assert (fs["a.png"]["w"], fs["a.png"]["h"], fs["a.png"]["pages"]) == (30, 20, 1)
    assert (fs["photo.JPG"]["w"], fs["photo.JPG"]["h"]) == (480, 640)  # EXIF rotation applied
    assert (fs["scan.tiff"]["w"], fs["scan.tiff"]["h"]) == (40, 20)  # first page
    assert fs["broken.gif"]["pages"] is None  # size unknown until the browser has tried
    assert fs["logo.ai"]["pages"] is None  # like any PDF
    assert all(f["intrinsic"] for f in fs.values())


def test_browser_measurement_beats_the_header(server):
    key = listed(server)["scan.tiff"]["key"]
    meta = json.dumps({"w": 40, "h": 20, "pages": 2}).encode()
    r = server.request("PUT", f"/thumb/{key}.json", body=meta, headers={"X-Esvigy": "1"})
    assert r.status == 204
    assert listed(server)["scan.tiff"]["pages"] == 2


@pytest.mark.parametrize("name, ctype", [
    ("a.png", "image/png"), ("photo.JPG", "image/jpeg"), ("c.bmp", "image/bmp"),
    ("scan.tiff", "image/tiff"), ("logo.ai", "application/pdf"), ("broken.gif", "image/gif"),
])
def test_files_are_served_with_their_type(server, name, ctype):
    r = server.request("GET", "/file/img/" + name)
    assert r.status == 200
    assert r.headers["Content-Type"] == ctype
    assert r.headers["Content-Security-Policy"] == esvigy.PDF_CSP
    assert r.body == (server.root / "img" / name).read_bytes()


def test_fix_is_ignored_for_images(server):
    assert server.request("GET", "/file/img/a.png?fix").body == (server.root / "img" / "a.png").read_bytes()


def test_tiff_decoder_is_served(server):
    for path in ("/static/tiffworker.js", "/static/tiffdoc.js", "/static/tiffcheck.js", "/static/vendor/utif/UTIF.js",
                 "/static/vendor/utif/pako_inflate.min.js"):
        r = server.request("GET", path)
        assert r.status == 200 and r.headers["Content-Type"].startswith("text/javascript"), path
