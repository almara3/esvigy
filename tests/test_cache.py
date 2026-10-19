"""Shrinking the thumbnail cache: by age, by size, or completely; by hand or at startup."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import esvigy

DAY = 86400
KEYS = ["%024x" % i for i in range(1, 10)]


def entry(cache, key, size=1000, age_days=0, meta=False):
    """A cached thumbnail (and optionally its metadata), last used `age_days` ago."""
    cache.mkdir(parents=True, exist_ok=True)
    t = time.time() - age_days * DAY
    paths = [cache / f"{key}.webp"] + ([cache / f"{key}.json"] if meta else [])
    for p in paths:
        p.write_bytes(b"x" * size)
        os.utime(p, (t, t))
    return paths


def names(cache):
    return sorted(p.name for p in cache.iterdir())


def test_prune_by_age_removes_thumbnail_and_metadata_together(cache):
    old = entry(cache, KEYS[0], age_days=40, meta=True)
    new = entry(cache, KEYS[1], age_days=5, meta=True)
    removed, freed = esvigy.prune_cache(max_age_days=30)
    assert (removed, freed) == (1, 2000)
    assert not any(p.exists() for p in old) and all(p.exists() for p in new)


def test_prune_by_size_removes_longest_unused_first(cache):
    for i, age in enumerate([1, 9, 3, 7, 5]):  # 1000 bytes each
        entry(cache, KEYS[i], age_days=age)
    removed, freed = esvigy.prune_cache(max_bytes=3500, target_bytes=2500)
    assert (removed, freed) == (3, 3000)
    assert names(cache) == [f"{KEYS[0]}.webp", f"{KEYS[2]}.webp"]  # used 1 and 3 days ago


def test_prune_below_size_limit_does_nothing(cache):
    entry(cache, KEYS[0])
    assert esvigy.prune_cache(max_bytes=5000, target_bytes=4000) == (0, 0)


def test_clear_removes_all_of_ours(cache):
    entry(cache, KEYS[0], meta=True)
    entry(cache, KEYS[1])
    assert esvigy.prune_cache(clear=True) == (2, 3000)
    assert names(cache) == []


def test_never_touches_anything_else(cache, tmp_path):
    entry(cache, KEYS[0], age_days=100)
    precious = tmp_path / "precious.webp"
    precious.write_bytes(b"keep me")
    others = {
        "notes.txt": b"mine",
        "0123456789abcdef0123456.webp": b"23 hex digits",
        "0123456789ABCDEF01234567.webp": b"upper case",
        "0123456789abcdef01234567.png": b"other type",
    }
    for n, data in others.items():
        (cache / n).write_bytes(data)
    (cache / "subdir").mkdir()
    (cache / f"{KEYS[1]}.webp").symlink_to(precious)  # named like ours, but a link
    old = time.time() - 100 * DAY
    for n in [*others, "subdir"]:
        os.utime(cache / n, (old, old))

    esvigy.prune_cache(clear=True)
    assert names(cache) == sorted([*others, "subdir", f"{KEYS[1]}.webp"])
    assert precious.read_bytes() == b"keep me"


def test_unfinished_uploads(cache):
    stale = cache / f".{KEYS[0]}.webp.12345"
    fresh = cache / f".{KEYS[1]}.json.678"
    cache.mkdir()
    for p, age in ((stale, 2), (fresh, 0)):
        p.write_bytes(b"half")
        t = time.time() - age * DAY
        os.utime(p, (t, t))
    esvigy.prune_cache(max_age_days=30)
    assert not stale.exists() and fresh.exists()  # a fresh one may be an upload in progress
    esvigy.prune_cache(clear=True)
    assert not fresh.exists()


def test_missing_cache_folder(cache):
    assert not cache.exists()
    assert esvigy.prune_cache(clear=True) == (0, 0)
    assert esvigy.cache_info() == (0, 0)


def test_cache_info(cache):
    entry(cache, KEYS[0], size=500, meta=True)
    entry(cache, KEYS[1], size=300)
    assert esvigy.cache_info() == (2, 1300)


# ---------------------------------------------------------------- "last used"

def test_listing_marks_thumbnails_used(server):
    key = next(f["key"] for f in server.get_json("/api/list")["files"] if f["name"] == "a.svg")
    (p,) = entry(server.cache, key, age_days=50)
    assert next(f for f in server.get_json("/api/list")["files"] if f["name"] == "a.svg")["thumb"]
    assert time.time() - p.stat().st_mtime < 60
    assert esvigy.prune_cache(max_age_days=30) == (0, 0)


def test_serving_marks_thumbnails_used(server):
    (p,) = entry(server.cache, KEYS[0], age_days=50)
    assert server.request("GET", f"/thumb/{KEYS[0]}.webp").status == 200
    assert time.time() - p.stat().st_mtime < 60


def test_recent_use_is_not_rewritten(server):
    # marking is at most once a day, so browsing doesn't cause a write per thumbnail per listing
    (p,) = entry(server.cache, KEYS[0], age_days=0.5)
    before = p.stat().st_mtime_ns
    server.request("GET", f"/thumb/{KEYS[0]}.webp")
    assert p.stat().st_mtime_ns == before


# ---------------------------------------------------------------- command line and startup

def run(args, cache):
    env = {**os.environ, "ESVIGY_CACHE": str(cache)}
    script = Path(esvigy.__file__)
    return subprocess.run([sys.executable, str(script), *args], env=env, capture_output=True, text=True, timeout=30)


def test_cli_cache_info(tmp_path):
    cache = tmp_path / "c"
    entry(cache, KEYS[0], size=2048)
    r = run(["--cache"], cache)
    assert r.returncode == 0
    assert str(cache) in r.stdout and "1 thumbnail" in r.stdout and "2.0 KB" in r.stdout


def test_cli_prune_and_clear(tmp_path):
    cache = tmp_path / "c"
    entry(cache, KEYS[0], age_days=40)
    entry(cache, KEYS[1], age_days=10)
    entry(cache, KEYS[2], age_days=1)
    r = run(["--prune-cache"], cache)  # default: unused for 30 days
    assert r.returncode == 0 and "removed 1 thumbnail" in r.stdout
    r = run(["--prune-cache", "7"], cache)
    assert "removed 1 thumbnail" in r.stdout
    assert names(cache) == [f"{KEYS[2]}.webp"]
    r = run(["--clear-cache"], cache)
    assert r.returncode == 0 and "removed 1 thumbnail" in r.stdout and names(cache) == []


def test_automatic_cleanup_limits(cache):
    entry(cache, KEYS[0], age_days=esvigy.AUTO_MAX_AGE_DAYS + 1)
    entry(cache, KEYS[1], age_days=1)
    assert esvigy.auto_prune_cache() == (1, 1000)
    assert names(cache) == [f"{KEYS[1]}.webp"]


@pytest.mark.parametrize("days", ["-5", "-0.1", "nan", "inf", "-inf", "abc"])
def test_cli_rejects_bad_days(tmp_path, days):
    cache = tmp_path / "c"
    (p,) = entry(cache, KEYS[0], age_days=0)
    r = run(["--prune-cache", days], cache)
    assert r.returncode == 2 and "DAYS" in r.stderr
    assert p.exists()


def test_cli_accepts_zero_days(tmp_path):
    cache = tmp_path / "c"
    entry(cache, KEYS[0], age_days=0.01)
    assert run(["--prune-cache", "0"], cache).returncode == 0
    assert names(cache) == []


# ---------------------------------------------------------------- cleanup while esvigy is running

def test_cleanup_keeps_what_was_marked_meanwhile(cache, monkeypatch):
    # The startup cleanup decides from a scan; a folder listed between that scan and the
    # deletion has reported its thumbnails as cached, so they must stay.
    (p,) = entry(cache, KEYS[0], age_days=100)
    real_scan = esvigy.cache_scan

    def scan_then_list():
        result = real_scan()
        assert esvigy.mark_used(p)  # the listing, just after the scan
        return result

    monkeypatch.setattr(esvigy, "cache_scan", scan_then_list)
    assert esvigy.auto_prune_cache() == (0, 0)
    assert p.exists()


def test_size_cleanup_skips_thumbnails_in_use(cache, monkeypatch):
    monkeypatch.setattr(esvigy, "in_use", set())
    paths = [entry(cache, KEYS[i], age_days=age)[0] for i, age in enumerate([0.2, 0.1])]
    esvigy.mark_used(paths[0])  # listed today, but the longest unused of the two
    assert esvigy.prune_cache(max_bytes=1500, target_bytes=1000) == (1, 1000)
    assert paths[0].exists() and not paths[1].exists()


def test_vanished_thumbnail_is_not_found(server, monkeypatch):
    (p,) = entry(server.cache, KEYS[0])
    real = esvigy.mark_used

    def mark_then_vanish(path):
        ok = real(path)
        path.unlink()  # removed (by another esvigy, or by hand) right after
        return ok

    monkeypatch.setattr(esvigy, "mark_used", mark_then_vanish)
    assert server.request("GET", f"/thumb/{KEYS[0]}.webp").status == 404
