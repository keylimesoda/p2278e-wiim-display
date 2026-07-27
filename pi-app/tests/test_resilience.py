"""Resilience tests for storage, health, and system telemetry.

No real hardware is stressed; throttle bits, sensors, and downloads are all
faked/injected.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from p2278e_display import (  # noqa: E402
    art_cache,
    audio_levels,
    audio_spectrum,
    health,
    system_health,
)
from p2278e_display.system_health import (  # noqa: E402
    BIT_UNDERVOLT_NOW,
    BIT_UNDERVOLT_STICKY,
    BIT_THROTTLED_NOW,
    BIT_SOFT_TEMP_NOW,
    BIT_THROTTLED_STICKY,
    PowerThermalSampler,
)

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None


def _png_bytes(size=(8, 8)) -> bytes:
    assert Image is not None
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------
# Health writer / reader
# --------------------------------------------------------------------------

def test_health_write_read_roundtrip(tmp_path):
    p = tmp_path / "run" / "health.json"
    w = health.HealthWriter(p)
    assert w.write({"updated_at": 123.0, "title": "x"}) is True
    r = health.read_health(p)
    assert r.ok and r["title"] == "x"


def test_health_reader_missing(tmp_path):
    r = health.read_health(tmp_path / "nope.json")
    assert not r.ok and r.error == "missing"


def test_health_reader_corrupt(tmp_path):
    p = tmp_path / "health.json"
    p.write_text("{not valid json")
    r = health.read_health(p)
    assert not r.ok and r.error.startswith("corrupt")


def test_health_reader_additive_fields(tmp_path):
    p = tmp_path / "health.json"
    p.write_text(json.dumps({"updated_at": 1, "future_field": {"a": 1}}))
    r = health.read_health(p)
    assert r.ok and r["future_field"] == {"a": 1}


def test_health_write_oserror_never_raises_and_rate_limits(tmp_path):
    # Point at a path whose parent is a file -> mkdir/replace raise OSError.
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    target = blocker / "sub" / "health.json"

    logs = []
    clock = {"t": 0.0}
    w = health.HealthWriter(
        target,
        log=logs.append,
        log_interval_s=60.0,
        clock=lambda: clock["t"],
    )
    # Many failing writes; must not raise.
    for _ in range(5):
        assert w.write({"updated_at": 1}) is False
    assert w.write_failed == 5
    # Rate limited: only one log within the interval.
    assert len(logs) == 1
    # Advance past interval -> one more log.
    clock["t"] = 120.0
    w.write({"updated_at": 1})
    assert len(logs) == 2


def test_health_write_is_atomic_no_temp_left(tmp_path):
    p = tmp_path / "health.json"
    w = health.HealthWriter(p)
    w.write({"updated_at": 1})
    leftovers = [x for x in tmp_path.iterdir() if x.name.startswith(".")]
    assert leftovers == []


# --------------------------------------------------------------------------
# Media-analysis cache privacy
# --------------------------------------------------------------------------

SIGNED_URI = "https://user:pass@example.invalid/music/track.flac?token=CANARY_SECRET#fragment"


def test_short_uri_redacts_credentials_query_and_fragment():
    label = audio_levels.short_uri(SIGNED_URI)
    assert label == "track.flac"
    assert "CANARY_SECRET" not in label
    assert "user" not in label


def test_level_cache_never_persists_signed_uri(tmp_path):
    cache = audio_levels.LevelCache(
        uri=SIGNED_URI,
        sample_rate=11025,
        hop_seconds=0.01,
        block_size=1024,
        mode=audio_levels.MODE,
        frames=[0.1, 0.2],
    )
    path = audio_levels.cache_path(tmp_path, SIGNED_URI)
    audio_levels.write_cache(path, cache)
    text = path.read_text()
    assert "CANARY_SECRET" not in text
    assert SIGNED_URI not in text
    assert path.stat().st_mode & 0o777 == 0o600
    loaded = audio_levels.load_cache(SIGNED_URI, tmp_path)
    assert loaded is not None and loaded.uri == SIGNED_URI


def test_spectrum_cache_never_persists_signed_uri(tmp_path):
    cache = audio_spectrum.SpectrumCache(
        uri=SIGNED_URI,
        sample_rate=48000,
        hop_seconds=0.01,
        block_size=2048,
        mode=audio_spectrum.MODE,
        bands=audio_spectrum.BANDS,
        frames=[[0.1] * audio_spectrum.BAND_COUNT],
    )
    path = audio_spectrum.cache_path(tmp_path, SIGNED_URI)
    audio_spectrum.write_cache(path, cache)
    text = path.read_text()
    assert "CANARY_SECRET" not in text
    assert SIGNED_URI not in text
    assert path.stat().st_mode & 0o777 == 0o600
    loaded = audio_spectrum.load_cache(SIGNED_URI, tmp_path)
    assert loaded is not None and loaded.uri == SIGNED_URI


# --------------------------------------------------------------------------
# Power / thermal sampler
# --------------------------------------------------------------------------

def test_missing_vcgencmd_is_nonfatal():
    s = PowerThermalSampler(
        throttled_provider=lambda: None,
        temp_provider=lambda: None,
    )
    out = s.sample()
    assert out["throttled_available"] is False
    assert out["power_warn"] is False
    assert out["thermal_warn"] is False
    assert out["temp_c"] is None


def test_sticky_history_never_raises_power_warn():
    # Only sticky undervoltage bit set (expected boot forensic), past grace.
    s = PowerThermalSampler(
        throttled_provider=lambda: BIT_UNDERVOLT_STICKY,
        temp_provider=lambda: 45.0,
        startup_grace_s=0.0,
        repeat_threshold=1,
    )
    for _ in range(5):
        out = s.sample()
    assert out["undervolt_sticky"] is True
    assert out["undervolt_now"] is False
    assert out["power_warn"] is False


def test_grace_suppresses_current_uv_then_warns():
    clock = {"t": 0.0}
    s = PowerThermalSampler(
        throttled_provider=lambda: BIT_UNDERVOLT_NOW,
        temp_provider=lambda: 50.0,
        startup_grace_s=10.0,
        repeat_threshold=3,
        clock=lambda: clock["t"],
    )
    # During grace: no warn even with repeated current UV.
    for _ in range(5):
        out = s.sample()
    assert out["in_grace"] is True
    assert out["power_warn"] is False

    # Past grace, streak must still reach threshold.
    clock["t"] = 20.0
    out = s.sample()  # streak continues (6), threshold 3 -> warn
    assert out["in_grace"] is False
    assert out["power_warn"] is True


def test_current_uv_streak_resets_on_clear():
    s = PowerThermalSampler(
        throttled_provider=lambda: BIT_UNDERVOLT_NOW,
        temp_provider=lambda: 50.0,
        startup_grace_s=0.0,
        repeat_threshold=3,
    )
    s.sample()
    s.sample()
    # Swap provider to clear UV.
    s._throttled_provider = lambda: 0
    out = s.sample()
    assert out["uv_streak"] == 0
    assert out["power_warn"] is False


def test_thermal_warn_from_current_bits():
    s = PowerThermalSampler(
        throttled_provider=lambda: BIT_THROTTLED_NOW | BIT_SOFT_TEMP_NOW,
        temp_provider=lambda: 82.0,
        startup_grace_s=0.0,
    )
    out = s.sample()
    assert out["throttled_now"] is True
    assert out["soft_temp_now"] is True
    assert out["thermal_warn"] is True


def test_realistic_boot_mask_0x50005_no_power_warn():
    # 0x50005 = bits 0,2,16,18 => current UV+throttle plus sticky history.
    # A single sample past grace should NOT warn (needs repeat streak).
    mask = 0x50005
    assert mask & BIT_UNDERVOLT_NOW
    assert mask & BIT_THROTTLED_NOW
    assert mask & BIT_UNDERVOLT_STICKY
    assert mask & BIT_THROTTLED_STICKY
    s = PowerThermalSampler(
        throttled_provider=lambda: mask,
        temp_provider=lambda: 60.0,
        startup_grace_s=0.0,
        repeat_threshold=3,
    )
    out = s.sample()
    assert out["power_warn"] is False  # only 1 sample
    assert out["thermal_warn"] is True  # current throttle bit set


def test_sample_returns_json_serializable():
    out = system_health.sample(
        throttled_provider=lambda: 0,
        temp_provider=lambda: 40.0,
    )
    json.dumps(out)  # must not raise


# --------------------------------------------------------------------------
# Art cache
# --------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, data: bytes, content_length=None):
        self._buf = io.BytesIO(data)
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def read(self, n=-1):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _opener_for(data, content_length=None):
    def _open(uri, timeout=None):
        return _FakeResp(data, content_length)
    return _open


@pytest.mark.skipif(Image is None, reason="Pillow required")
def test_cache_valid_image(tmp_path):
    data = _png_bytes()
    p = art_cache.cache_album_art(
        "https://x/y.jpg", tmp_path, opener=_opener_for(data, len(data))
    )
    assert p and Path(p).exists()
    # Second call hits cache (validated existing).
    p2 = art_cache.cache_album_art(
        "https://x/y.jpg", tmp_path, opener=_opener_for(b"junk")
    )
    assert p2 == p


def test_cache_corrupt_download_fails_soft(tmp_path):
    p = art_cache.cache_album_art(
        "https://x/y.jpg", tmp_path, opener=_opener_for(b"not an image")
    )
    assert p == ""
    # No leftover temp/cache files.
    files = list(tmp_path.iterdir())
    assert files == []


def test_cache_oversized_content_length(tmp_path):
    big = b"\x00" * 10
    p = art_cache.cache_album_art(
        "https://x/y.jpg",
        tmp_path,
        max_bytes=5,
        opener=_opener_for(big, content_length=999),
    )
    assert p == ""


def test_cache_oversized_stream_cap_no_header(tmp_path):
    # Lying/absent header: cap enforced by streaming.
    big = b"\x00" * 1000
    p = art_cache.cache_album_art(
        "https://x/y.jpg",
        tmp_path,
        max_bytes=100,
        opener=_opener_for(big, content_length=None),
    )
    assert p == ""


@pytest.mark.skipif(Image is None, reason="Pillow required")
def test_cache_replaces_corrupt_existing(tmp_path):
    key = art_cache.cache_album_art  # ensure module imported
    # Pre-seed a corrupt cache file at the expected path.
    import hashlib

    uri = "https://x/z.jpg"
    path = tmp_path / (hashlib.sha256(uri.encode()).hexdigest()[:24] + ".jpg")
    path.write_bytes(b"corrupt")
    data = _png_bytes()
    p = key(uri, tmp_path, opener=_opener_for(data, len(data)))
    assert p == str(path)
    assert art_cache.validate_image_bytes(path.read_bytes())


@pytest.mark.skipif(Image is None, reason="Pillow required")
def test_prune_by_count(tmp_path):
    for i in range(5):
        (tmp_path / f"f{i}.jpg").write_bytes(_png_bytes())
    removed = art_cache.prune_cache(tmp_path, max_files=2,
                                    max_total_bytes=10**9,
                                    max_age_s=10**9)
    assert len(removed) == 3
    assert len([x for x in tmp_path.iterdir() if x.is_file()]) == 2


def test_prune_by_age(tmp_path):
    import os
    old = tmp_path / "old.jpg"
    old.write_bytes(b"x" * 10)
    os.utime(old, (0, 0))  # epoch mtime -> very old
    new = tmp_path / "new.jpg"
    new.write_bytes(b"x" * 10)
    removed = art_cache.prune_cache(tmp_path, max_files=100,
                                    max_total_bytes=10**9, max_age_s=3600)
    assert str(old) in removed
    assert new.exists()


def test_prune_by_bytes(tmp_path):
    import os
    import time as _t
    base = _t.time()
    files = []
    for i in range(4):
        f = tmp_path / f"f{i}.jpg"
        f.write_bytes(b"x" * 100)
        os.utime(f, (base + i, base + i))  # ascending, recent mtime
        files.append(f)
    # cap 250 bytes -> keep newest 2 (200 bytes), drop oldest 2.
    removed = art_cache.prune_cache(tmp_path, max_files=100,
                                    max_total_bytes=250, max_age_s=10**9)
    assert len(removed) == 2
    removed_names = {Path(r).name for r in removed}
    assert removed_names == {"f0.jpg", "f1.jpg"}


def test_prune_ignores_temp_and_dirs(tmp_path):
    (tmp_path / ".hidden.tmp").write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    removed = art_cache.prune_cache(tmp_path, max_files=100,
                                    max_total_bytes=10**9, max_age_s=10**9)
    assert removed == []


def test_empty_uri_returns_empty(tmp_path):
    assert art_cache.cache_album_art("", tmp_path) == ""
