import gzip
import http.client
import json
import struct
import threading
import zlib
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


def make_png(path, w, h, rgb):
    """Write a valid PNG filled with one colour."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\0" + bytes(rgb) * w for _ in range(h))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    return path


def make_bmp(path, w, h, rgb):
    """Write a valid 24-bit BMP filled with one colour."""
    row = bytes(rgb[::-1]) * w
    row += b"\0" * (-len(row) % 4)
    pixels = row * h
    path.write_bytes(b"BM" + struct.pack("<IHHI", 54 + len(pixels), 0, 0, 54)
                     + struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(pixels), 2835, 2835, 0, 0) + pixels)
    return path


def make_tiff(path, pages, big_endian=False):
    """Write an uncompressed RGB TIFF. `pages` is a list of (w, h, (r, g, b)).

    The image directories come after the pixel data, at the end of the file, as many writers do.
    """
    e = ">" if big_endian else "<"
    out = bytearray(b"MM\0*" if big_endian else b"II*\0") + b"\0\0\0\0"
    strips = []
    for w, h, rgb in pages:
        strips.append(len(out))
        out += bytes(rgb) * (w * h)
    link = 4  # where the offset of the next directory goes
    for (w, h, _), strip in zip(pages, strips):
        if len(out) % 2:
            out += b"\0"
        struct.pack_into(e + "I", out, link, len(out))
        bps = len(out) + 2 + 12 * 9 + 4  # BitsPerSample values follow the directory
        tags = [(256, 4, 1, w), (257, 4, 1, h), (258, 3, 3, bps), (259, 3, 1, 1), (262, 3, 1, 2),
                (273, 4, 1, strip), (277, 3, 1, 3), (278, 4, 1, h), (279, 4, 1, w * h * 3)]
        out += struct.pack(e + "H", len(tags))
        for tag, typ, n, v in tags:
            out += struct.pack(e + "HHI", tag, typ, n)
            out += struct.pack(e + "HH", v, 0) if typ == 3 and n == 1 else struct.pack(e + "I", v)
        link = len(out)
        out += b"\0\0\0\0" + struct.pack(e + "HHH", 8, 8, 8)
    path.write_bytes(bytes(out))
    return path



def tiff_bytes(w, h, data=b"", compression=1, bits=(8,), photometric=1, extra=(), next_ifd=0):
    """A little-endian TIFF with one directory whose tags may lie, for the decoder limit tests.

    `extra` adds (tag, type, count, value) entries; a bytes value is stored after the strip and
    pointed to (or kept in the entry if it fits). `next_ifd` is the next directory's offset
    (8 = this one again: a loop). The strip `data` follows the directory.
    """
    tags = [(256, 4, 1, w), (257, 4, 1, h), (258, 3, len(bits), None), (259, 3, 1, compression),
            (262, 3, 1, photometric), (273, 4, 1, None), (277, 3, 1, len(bits)), (278, 4, 1, h),
            (279, 4, 1, len(data))] + list(extra)
    tags.sort(key=lambda t: t[0])
    bits_off = 8 + 2 + 12 * len(tags) + 4
    bits_blob = struct.pack("<%dH" % len(bits), *bits) if len(bits) > 2 else b""
    data_off = bits_off + len(bits_blob)
    blobs, blob_off = b"", data_off + len(data)
    out = b"II*\0" + struct.pack("<IH", 8, len(tags))
    for tag, typ, cnt, val in tags:
        if tag == 258:
            v = struct.pack("<I", bits_off) if bits_blob else struct.pack("<HH", *(list(bits) + [0])[:2])
        elif tag == 273:
            v = struct.pack("<I", data_off)
        elif isinstance(val, bytes):
            if len(val) <= 4:
                v = val.ljust(4, b"\0")
            else:
                v = struct.pack("<I", blob_off + len(blobs))
                blobs += val
        elif typ == 3 and cnt == 1:
            v = struct.pack("<HH", val, 0)
        else:
            v = struct.pack("<I", val)
        out += struct.pack("<HHI", tag, typ, cnt) + v
    return out + struct.pack("<I", next_ifd) + bits_blob + data + blobs

def make_jpeg_header(w, h, orientation=None, big_endian=False):
    """The start of a JPEG (enough to measure it): SOI, optional EXIF orientation, SOF0."""
    out = b"\xff\xd8"
    if orientation:
        e = ">" if big_endian else "<"
        tiff = (b"MM\0*" if big_endian else b"II*\0") + struct.pack(e + "IH", 8, 1) \
            + struct.pack(e + "HHIHH", 0x0112, 3, 1, orientation, 0) + b"\0\0\0\0"
        seg = b"Exif\0\0" + tiff
        out += b"\xff\xe1" + struct.pack(">H", len(seg) + 2) + seg
    out += b"\xff\xdb" + struct.pack(">H", 67) + b"\0" * 65  # a quantisation table, skipped
    out += b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\1\x11\0"
    return out + b"\xff\xd9"


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
