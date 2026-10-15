"""Unit tests for the SVG header parsing helpers."""

import gzip
import os

import pytest

import esvigy


@pytest.mark.parametrize("value, expected", [
    ("100", 100),
    ("10px", 10),
    (" 12.5 ", 12.5),
    ("1in", 96),
    ("25.4mm", 96),
    ("2.54cm", 96),
    ("12pt", 16),
    ("1e2", 100),
])
def test_parse_length(value, expected):
    assert esvigy.parse_length(value) == pytest.approx(expected)


@pytest.mark.parametrize("value", ["50%", "", None, "0", "-5", "abc", "10 px px", "auto"])
def test_parse_length_rejects(value):
    assert esvigy.parse_length(value) is None


@pytest.mark.parametrize("tag, expected", [
    # explicit size
    ('<svg width="200" height="100">', (200, 100, True)),
    ("<svg width='2in' height='1in'>", (192, 96, True)),
    # only a viewBox: size comes from it, but the file has no intrinsic size
    ('<svg viewBox="0 0 100 50">', (100, 50, False)),
    ('<svg viewBox="0,0,100,50">', (100, 50, False)),
    # one dimension plus viewBox: the other follows the aspect ratio
    ('<svg width="400" viewBox="0 0 100 50">', (400, 200, False)),
    ('<svg height="100" viewBox="0 0 100 50">', (200, 100, False)),
    # percentages can't be resolved, fall back to the viewBox
    ('<svg width="100%" height="100%" viewBox="0 0 30 20">', (30, 20, False)),
    # nothing usable: browser default size
    ("<svg>", (300, 150, False)),
    ('<svg viewBox="0 0 0 0">', (300, 150, False)),
])
def test_svg_size(tag, expected):
    head = b'<?xml version="1.0"?>\n<!-- comment -->\n' + tag.encode() + b"<rect/></svg>"
    w, h, intrinsic = esvigy.svg_size(head)
    assert (w, h) == pytest.approx(expected[:2])
    assert intrinsic is expected[2]


def test_svg_size_attributes_over_several_lines():
    head = b'<svg\n  xmlns="http://www.w3.org/2000/svg"\n  width="20"\n  height="10"\n>'
    assert esvigy.svg_size(head) == (20, 10, True)


def test_svg_size_without_svg_tag():
    assert esvigy.svg_size(b"not an svg") == (300, 150, False)


def test_with_intrinsic_size_adds_width_and_height():
    head = b'<?xml version="1.0"?><svg viewBox="0 0 100 50"><rect stroke-width="3"/></svg>'
    out = esvigy.with_intrinsic_size(head, 100, 50)
    assert esvigy.svg_size(out) == (100, 50, True)
    assert out.startswith(b'<?xml version="1.0"?><svg width="100.000" height="50.000" ')
    # only the root tag changes; similar-looking attributes elsewhere are untouched
    assert out.endswith(b'<rect stroke-width="3"/></svg>')


def test_with_intrinsic_size_replaces_partial_size():
    head = b'<svg width="100%" viewBox="0 0 10 10" stroke-width="2">'
    out = esvigy.with_intrinsic_size(head, 10, 10)
    assert out.count(b" width=") == 1
    assert b'stroke-width="2"' in out
    assert esvigy.svg_size(out) == (10, 10, True)


def test_with_intrinsic_size_leaves_non_svg_alone():
    assert esvigy.with_intrinsic_size(b"hello", 1, 1) == b"hello"


def test_read_head_plain_and_gzip(tmp_path):
    data = b'<svg width="5" height="5"/>'
    (tmp_path / "a.svg").write_bytes(data)
    (tmp_path / "b.svgz").write_bytes(gzip.compress(data))
    assert esvigy.read_head(tmp_path / "a.svg") == data
    assert esvigy.read_head(tmp_path / "b.svgz") == data


def test_read_head_is_limited_and_tolerates_garbage(tmp_path):
    (tmp_path / "big.svg").write_bytes(b"x" * (esvigy.HEAD_BYTES * 2))
    (tmp_path / "bad.svgz").write_bytes(b"not gzip")
    assert len(esvigy.read_head(tmp_path / "big.svg")) == esvigy.HEAD_BYTES
    assert esvigy.read_head(tmp_path / "bad.svgz") == b""


def test_thumb_key_changes_with_file(tmp_path):
    p = tmp_path / "a.svg"
    p.write_text("<svg/>")
    k1 = esvigy.thumb_key(p, p.stat())
    assert len(k1) == 24 and int(k1, 16) >= 0
    assert esvigy.thumb_key(p, p.stat()) == k1

    p.write_text("<svg></svg>")  # different size
    k2 = esvigy.thumb_key(p, p.stat())
    os.utime(p, ns=(0, 1_000_000_000))  # different mtime
    k3 = esvigy.thumb_key(p, p.stat())
    assert len({k1, k2, k3}) == 3
