from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import threading
import time
from typing import Callable

from p2278e_display.audio_levels import parse_hms, sample_linear, short_uri

SAMPLE_RATE = 48000
BLOCK_SIZE = 2048
HOP_SIZE = 800
HOP_SECONDS = HOP_SIZE / SAMPLE_RATE
BAND_COUNT = 9
MAX_CACHE_FILES = 24
MAX_CACHE_AGE_SECONDS = 14 * 24 * 60 * 60
MODE = "native_spectrum_fft_v2_octave2048_calibrated"
BANDS = [
    (40, 80),
    (80, 160),
    (160, 320),
    (320, 640),
    (640, 1250),
    (1250, 2500),
    (2500, 5000),
    (5000, 10000),
    (10000, 16000),
]
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ANALYZER_BIN = PROJECT_ROOT / "bin" / "spectrum_analyzer"


@dataclass
class SpectrumCache:
    uri: str
    sample_rate: int
    hop_seconds: float
    block_size: int
    mode: str
    bands: list[tuple[int, int]]
    frames: list[list[float]]

    def levels_at(self, rel_time: str) -> tuple[float, ...]:
        seconds = parse_hms(rel_time)
        if seconds is None:
            return ()
        return self.levels_at_seconds(seconds)

    def levels_at_seconds(self, seconds: float) -> tuple[float, ...]:
        if not self.frames:
            return ()
        end_pos = max(0.0, min(len(self.frames) - 1, seconds / self.hop_seconds))
        lo = int(end_pos)
        hi = min(len(self.frames) - 1, lo + 1)
        frac = end_pos - lo
        out: list[float] = []
        for band in range(BAND_COUNT):
            a = self.frames[lo][band] if band < len(self.frames[lo]) else 0.0
            b = self.frames[hi][band] if band < len(self.frames[hi]) else a
            out.append(float(a * (1.0 - frac) + b * frac))
        return tuple(out)


class AsyncSpectrumAnalyzer:
    """Non-blocking per-track 9-band native FFT spectrum cache manager."""

    def __init__(self, cache_dir: Path, logger: Callable[[str], None] | None = None):
        self.cache_dir = cache_dir
        self.logger = logger or (lambda _msg: None)
        self.current_uri = ""
        self.current_cache: SpectrumCache | None = None
        self.pending_uri = ""
        self.worker: threading.Thread | None = None
        self.lock = threading.Lock()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        prune_cache(self.cache_dir)

    def request(self, uri: str) -> SpectrumCache | None:
        if not uri:
            with self.lock:
                self.current_uri = ""
                self.current_cache = None
                self.pending_uri = ""
            return None

        with self.lock:
            if uri == self.current_uri:
                return self.current_cache
            if uri == self.pending_uri:
                return None

        cached = load_cache(uri, self.cache_dir)
        if cached:
            with self.lock:
                self.current_uri = uri
                self.current_cache = cached
                self.pending_uri = ""
            self.logger(f"spectrum cache hit frames={len(cached.frames)} uri={short_uri(uri)}")
            return cached

        if not ffmpeg_available():
            self.logger("spectrum analysis disabled: ffmpeg not available")
            return None
        if not ANALYZER_BIN.exists():
            self.logger(f"spectrum analysis disabled: native analyzer missing at {ANALYZER_BIN}")
            return None

        with self.lock:
            self.pending_uri = uri
        self.worker = threading.Thread(target=self._worker, args=(uri,), daemon=True, name="p2278e-spectrum-analyzer")
        self.worker.start()
        self.logger(f"spectrum analysis queued uri={short_uri(uri)}")
        return None

    def levels_at(self, uri: str, rel_time: str) -> tuple[float, ...]:
        cache = self.request(uri)
        return cache.levels_at(rel_time) if cache else ()

    def levels_at_seconds(self, uri: str, seconds: float) -> tuple[float, ...]:
        cache = self.request(uri)
        return cache.levels_at_seconds(seconds) if cache else ()

    def _worker(self, uri: str) -> None:
        try:
            t0 = time.monotonic()
            cache = analyze_uri(uri)
            path = cache_path(self.cache_dir, uri)
            tmp = path.with_suffix(".tmp")
            write_cache(path, cache)
            prune_cache(self.cache_dir)
            with self.lock:
                if self.pending_uri == uri:
                    self.current_uri = uri
                    self.current_cache = cache
                    self.pending_uri = ""
            self.logger(f"spectrum analysis ready frames={len(cache.frames)} seconds={time.monotonic() - t0:.2f} uri={short_uri(uri)}")
        except Exception as exc:
            with self.lock:
                if self.pending_uri == uri:
                    self.pending_uri = ""
            self.logger(f"spectrum analysis failed {type(exc).__name__}")


def cache_key(uri: str) -> str:
    return hashlib.sha256(uri.encode("utf-8")).hexdigest()[:24]


def cache_path(cache_dir: Path, uri: str) -> Path:
    return cache_dir / f"{cache_key(uri)}.spectrum.json"


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def load_cache(uri: str, cache_dir: Path) -> SpectrumCache | None:
    path = cache_path(cache_dir, uri)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if not data.get("frames"):
            return None
        if data.get("mode") != MODE:
            return None
        path.touch()
        return SpectrumCache(
            uri=uri,
            sample_rate=int(data["sample_rate"]),
            hop_seconds=float(data["hop_seconds"]),
            block_size=int(data.get("block_size", BLOCK_SIZE)),
            mode=str(data.get("mode", MODE)),
            bands=[tuple(map(int, b)) for b in data.get("bands", BANDS)],
            frames=[[float(x) for x in frame[:BAND_COUNT]] for frame in data["frames"]],
        )
    except Exception:
        return None


def write_cache(path: Path, cache: SpectrumCache) -> None:
    """Atomically persist analysis without writing the source/signed media URL."""
    payload = {
        "sample_rate": cache.sample_rate,
        "hop_seconds": cache.hop_seconds,
        "block_size": cache.block_size,
        "mode": cache.mode,
        "bands": cache.bands,
        "frames": cache.frames,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")))
    tmp.chmod(0o600)
    tmp.replace(path)


def prune_cache(cache_dir: Path, max_files: int = MAX_CACHE_FILES, max_age_seconds: int = MAX_CACHE_AGE_SECONDS) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for path in list(cache_dir.glob("*.spectrum.json")):
        try:
            if now - path.stat().st_mtime > max_age_seconds:
                path.unlink(missing_ok=True)
        except OSError:
            pass
    files = sorted(cache_dir.glob("*.spectrum.json"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    for path in files[max_files:]:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    for path in cache_dir.glob("*.tmp"):
        try:
            if now - path.stat().st_mtime > 10 * 60:
                path.unlink(missing_ok=True)
        except OSError:
            pass


def analyze_uri(uri: str) -> SpectrumCache:
    # ffmpeg handles network/file decode; native C++ handles the rolling-window FFT.
    ffmpeg_cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        uri,
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-f",
        "f32le",
        "-",
    ]
    analyzer_cmd = [
        str(ANALYZER_BIN),
        "--sample-rate",
        str(SAMPLE_RATE),
        "--window",
        str(BLOCK_SIZE),
        "--hop",
        str(HOP_SIZE),
        "--attack",
        "0.68",
        "--release",
        "0.14",
    ]
    ffmpeg = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    analyzer = subprocess.Popen(analyzer_cmd, stdin=ffmpeg.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert ffmpeg.stdout is not None
    ffmpeg.stdout.close()
    try:
        out_bytes, analyzer_err_bytes = analyzer.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        analyzer.kill()
        ffmpeg.kill()
        analyzer.communicate()
        ffmpeg.communicate()
        raise RuntimeError("spectrum analysis timed out") from None
    out = out_bytes.decode("utf-8", errors="replace")
    analyzer_err = analyzer_err_bytes.decode("utf-8", errors="replace")
    ffmpeg_err = ffmpeg.stderr.read().decode("utf-8", errors="replace") if ffmpeg.stderr else ""
    ffmpeg_rc = ffmpeg.wait(timeout=10)
    if ffmpeg_rc != 0:
        raise RuntimeError(f"ffmpeg failed rc={ffmpeg_rc}")
    if analyzer.returncode != 0:
        raise RuntimeError(f"native spectrum analyzer failed rc={analyzer.returncode}: {analyzer_err.strip()[:240]}")
    data = json.loads(out)
    frames = [[round(float(x), 3) for x in frame[:BAND_COUNT]] for frame in data.get("frames", [])]
    return SpectrumCache(
        uri=uri,
        sample_rate=int(data.get("sample_rate", SAMPLE_RATE)),
        hop_seconds=float(data.get("hop_seconds", HOP_SECONDS)),
        block_size=int(data.get("block_size", BLOCK_SIZE)),
        mode=str(data.get("mode", MODE)),
        bands=[tuple(map(int, b)) for b in data.get("bands", BANDS)],
        frames=frames,
    )
