from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import math
import os
import selectors
import shutil
import signal
import subprocess
import time
from urllib.parse import unquote, urlsplit

from p2278e_display.analysis_worker import AnalysisWorker, signal_process_group
from p2278e_display.cache_io import write_bounded_json

import numpy as np

SAMPLE_RATE = 11025
BLOCK_SIZE = 1024
# Keep cached envelope at display-ish cadence so 60 FPS rendering can interpolate smoothly.
HOP_SIZE = max(1, round(SAMPLE_RATE / 60))
HOP_SECONDS = HOP_SIZE / SAMPLE_RATE
HISTORY_COLUMNS = 9
MAX_ANALYSIS_SECONDS = 180
MAX_ANALYSIS_PCM_BYTES = 16_000_000
MAX_FFMPEG_OUTPUT_BYTES = MAX_ANALYSIS_PCM_BYTES - 1_000_000
MAX_CACHE_ENTRY_BYTES = 8_000_000
MAX_CACHE_TOTAL_BYTES = 32_000_000
MAX_CACHE_FILES = 24
MAX_CACHE_AGE_SECONDS = 14 * 24 * 60 * 60
EPSILON = 1e-10
DB_FLOOR = -60.0
ATTACK_SECONDS = 0.025
RELEASE_SECONDS = 0.180
MODE = "peak_rms_meter_v3_60fps"


@dataclass
class LevelCache:
    uri: str
    sample_rate: int
    hop_seconds: float
    block_size: int
    mode: str
    frames: list[float]

    def levels_at(self, rel_time: str) -> tuple[float, ...]:
        """Return 9 recent full-band level-history columns ending at rel_time.

        This is not a frequency display. It is a simple peak/RMS meter history:
        left = slightly older, right = newest/current.
        """
        seconds = parse_hms(rel_time)
        if seconds is None:
            return ()
        return self.levels_at_seconds(seconds)

    def levels_at_seconds(self, seconds: float) -> tuple[float, ...]:
        """Return recent meter history ending at a floating-point playback time."""
        if not self.frames or seconds < 0 or seconds > len(self.frames) * self.hop_seconds:
            return ()
        end_pos = max(0.0, min(len(self.frames) - 1, seconds / self.hop_seconds))
        vals: list[float] = []
        for col in range(HISTORY_COLUMNS):
            pos = end_pos - (HISTORY_COLUMNS - 1 - col)
            vals.append(sample_linear(self.frames, pos))
        return tuple(vals)


class AsyncLevelAnalyzer(AnalysisWorker):
    """At most one active analysis and one latest queued URI."""

    def _load(self, uri, cache_dir):
        return load_cache(uri, cache_dir)

    def _analyze(self, uri, register_process):
        return analyze_uri(uri, register_process=register_process)

    def _available(self):
        return ffmpeg_available()

    def _path(self, cache_dir, uri):
        return cache_path(cache_dir, uri)

    def _write(self, path, cache):
        write_cache(path, cache)

    def _prune(self, cache_dir):
        prune_cache(cache_dir)


def parse_hms(value: str) -> float | None:
    if not value or value == "NOT_IMPLEMENTED":
        return None
    parts = value.split(":")
    if len(parts) != 3:
        return None
    try:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    except ValueError:
        return None


def sample_linear(frames: list[float], pos: float) -> float:
    if not frames:
        return 0.0
    if pos <= 0:
        return frames[0]
    if pos >= len(frames) - 1:
        return frames[-1]
    lo = int(math.floor(pos))
    hi = lo + 1
    frac = pos - lo
    return float(frames[lo] * (1.0 - frac) + frames[hi] * frac)


def short_uri(uri: str) -> str:
    """Return a non-secret label: URL query, fragment and userinfo never leak."""
    parts = urlsplit(uri)
    name = unquote(parts.path.rsplit("/", 1)[-1]).strip()
    return name[:80] if name else f"track-{cache_key(uri)[:8]}"


def cache_key(uri: str) -> str:
    return hashlib.sha256(uri.encode("utf-8")).hexdigest()[:24]


def cache_path(cache_dir: Path, uri: str) -> Path:
    return cache_dir / f"{cache_key(uri)}.levels.json"


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def load_cache(uri: str, cache_dir: Path) -> LevelCache | None:
    path = cache_path(cache_dir, uri)
    if not path.exists():
        return None
    try:
        if path.stat().st_size > MAX_CACHE_ENTRY_BYTES:
            return None
        data = json.loads(path.read_text())
        if not data.get("frames"):
            return None
        if data.get("mode") != MODE:
            return None
        path.touch()
        return LevelCache(
            uri=uri,
            sample_rate=int(data["sample_rate"]),
            hop_seconds=float(data["hop_seconds"]),
            block_size=int(data.get("block_size", BLOCK_SIZE)),
            mode=str(data.get("mode", MODE)),
            frames=[float(x) for x in data["frames"]],
        )
    except Exception:
        return None


def load_or_analyze(uri: str, cache_dir: Path) -> LevelCache | None:
    if not uri:
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = load_cache(uri, cache_dir)
    if cached:
        return cached
    if not ffmpeg_available():
        return None
    cache = analyze_uri(uri)
    path = cache_path(cache_dir, uri)
    write_cache(path, cache)
    prune_cache(cache_dir)
    return cache


def write_cache(path: Path, cache: LevelCache) -> None:
    """Atomically persist analysis without writing the source/signed media URL."""
    payload = {
        "sample_rate": cache.sample_rate,
        "hop_seconds": cache.hop_seconds,
        "block_size": cache.block_size,
        "mode": cache.mode,
        "frames": cache.frames,
    }
    write_bounded_json(path, payload, MAX_CACHE_ENTRY_BYTES)


def prune_cache(cache_dir: Path, max_files: int = MAX_CACHE_FILES, max_age_seconds: int = MAX_CACHE_AGE_SECONDS, max_total_bytes: int = MAX_CACHE_TOTAL_BYTES) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for path in list(cache_dir.glob("*.levels.json")):
        try:
            if now - path.stat().st_mtime > max_age_seconds:
                path.unlink(missing_ok=True)
        except OSError:
            pass
    files = sorted(cache_dir.glob("*.levels.json"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    for path in files[max_files:]:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    total = 0
    for path in files[:max_files]:
        try:
            size = path.stat().st_size
            if total + size > max_total_bytes or size > MAX_CACHE_ENTRY_BYTES:
                path.unlink(missing_ok=True)
            else:
                total += size
        except OSError:
            pass
    for path in cache_dir.glob("*.tmp"):
        try:
            if now - path.stat().st_mtime > 10 * 60:
                path.unlink(missing_ok=True)
        except OSError:
            pass


def analysis_duration_limit() -> float:
    """Duration cap whose mono float32 decode fits the PCM byte budget."""
    return min(MAX_ANALYSIS_SECONDS, MAX_FFMPEG_OUTPUT_BYTES / (SAMPLE_RATE * 4))


def _read_bounded_pcm(proc, *, limit: int, timeout: float) -> memoryview:
    """Read stdout into a fixed-size buffer, never exceeding the PCM limit."""
    output = bytearray(limit)
    output_view = memoryview(output)
    written = 0
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                signal_process_group(proc, signal.SIGKILL)
                try:
                    proc.wait(timeout=1)
                except (subprocess.TimeoutExpired, OSError):
                    pass
                raise TimeoutError("level analysis timed out")
            remaining_bytes = limit - written
            chunk = os.read(proc.stdout.fileno(), min(64 * 1024, remaining_bytes + 1))
            if not chunk:
                break
            if len(chunk) > remaining_bytes:
                signal_process_group(proc, signal.SIGKILL)
                try:
                    proc.wait(timeout=1)
                except (subprocess.TimeoutExpired, OSError):
                    pass
                raise RuntimeError("decoded audio exceeded PCM byte budget")
            output_view[written : written + len(chunk)] = chunk
            written += len(chunk)
        proc.wait(timeout=max(0.001, deadline - time.monotonic()))
        return output_view[:written]
    except subprocess.TimeoutExpired:
        signal_process_group(proc, signal.SIGKILL)
        try:
            proc.wait(timeout=1)
        except (subprocess.TimeoutExpired, OSError):
            pass
        raise TimeoutError("level analysis timed out") from None
    finally:
        selector.close()


def analyze_uri(uri: str, register_process=None) -> LevelCache:
    # ffmpeg's mono float32 output is capped by both duration and decoded bytes.
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        uri,
        "-t",
        f"{analysis_duration_limit():.6f}",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-f",
        "f32le",
        "-fs",
        str(MAX_FFMPEG_OUTPUT_BYTES),
        "-",
    ]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True
    )
    try:
        if register_process:
            register_process(proc)
        raw = _read_bounded_pcm(
            proc, limit=MAX_ANALYSIS_PCM_BYTES, timeout=90
        )
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed rc={proc.returncode}")
        if len(raw) % np.dtype(np.float32).itemsize:
            raise RuntimeError("ffmpeg returned incomplete float32 PCM")
    finally:
        if proc.poll() is None:
            signal_process_group(proc, signal.SIGKILL)
        try:
            proc.wait(timeout=1)
        except (subprocess.TimeoutExpired, OSError):
            pass
    samples = np.frombuffer(raw, dtype=np.float32)
    if samples.size == 0:
        return LevelCache(uri=uri, sample_rate=SAMPLE_RATE, hop_seconds=HOP_SECONDS, block_size=BLOCK_SIZE, mode=MODE, frames=[])
    samples = np.nan_to_num(samples, copy=False)
    frames = compute_level_frames(samples)
    return LevelCache(uri=uri, sample_rate=SAMPLE_RATE, hop_seconds=HOP_SECONDS, block_size=BLOCK_SIZE, mode=MODE, frames=frames)


def compute_level_frames(samples: np.ndarray) -> list[float]:
    """Fast vectorized block peak/RMS meter with attack/release ballistics."""
    if samples.size < BLOCK_SIZE:
        samples = np.pad(samples, (0, BLOCK_SIZE - samples.size))
    frame_count = 1 + max(0, (samples.size - BLOCK_SIZE) // HOP_SIZE)
    if frame_count <= 0:
        return []

    shape = (frame_count, BLOCK_SIZE)
    strides = (samples.strides[0] * HOP_SIZE, samples.strides[0])
    blocks = np.lib.stride_tricks.as_strided(samples, shape=shape, strides=strides)

    peak = np.max(np.abs(blocks), axis=1)
    rms = np.sqrt(np.mean(blocks * blocks, axis=1))
    peak_db = 20.0 * np.log10(peak + EPSILON)
    rms_db = 20.0 * np.log10(rms + EPSILON)

    # Peak gives the blink; RMS gives body. This is a deliberately simple meter.
    current_db = np.maximum(rms_db + 7.0, peak_db - 8.0)
    current_db = np.clip(current_db, DB_FLOOR, 0.0)

    attack = math.exp(-HOP_SECONDS / ATTACK_SECONDS)
    release = math.exp(-HOP_SECONDS / RELEASE_SECONDS)
    env = DB_FLOOR
    out = np.empty_like(current_db, dtype=np.float32)
    for i, val in enumerate(current_db):
        coef = attack if val > env else release
        env = coef * env + (1.0 - coef) * float(val)
        out[i] = env

    # Normalize per track from the actual envelope distribution. This is the
    # important tuning step for modern compressed music: if a track lives in a
    # narrow loudness range, expand that range instead of pegging every column
    # into red all the time.
    floor = float(np.percentile(out, 12))
    ceiling = float(np.percentile(out, 97))
    if ceiling - floor < 6.0:
        mid = (ceiling + floor) * 0.5
        floor = mid - 3.0
        ceiling = mid + 3.0
    linear = np.clip((out - floor) / max(1e-6, ceiling - floor), 0.0, 1.0)
    # Mild gamma: responsive, but red remains peak territory.
    linear = np.power(linear, 1.10)
    return [round(float(x), 3) for x in linear]
