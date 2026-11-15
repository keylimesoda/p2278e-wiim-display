from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import shutil
import signal
import subprocess
import time

from p2278e_display.analysis_worker import AnalysisWorker, signal_process_group
from p2278e_display.cache_io import write_bounded_json
from p2278e_display.audio_levels import parse_hms, sample_linear, short_uri

SAMPLE_RATE = 48000
BLOCK_SIZE = 2048
HOP_SIZE = 800
HOP_SECONDS = HOP_SIZE / SAMPLE_RATE
BAND_COUNT = 9
MAX_ANALYSIS_SECONDS = 180
MAX_ANALYSIS_PCM_BYTES = 16_000_000
MAX_FFMPEG_OUTPUT_BYTES = MAX_ANALYSIS_PCM_BYTES - 1_000_000
MAX_CACHE_ENTRY_BYTES = 8_000_000
MAX_CACHE_TOTAL_BYTES = 32_000_000
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
        if not self.frames or seconds < 0 or seconds > len(self.frames) * self.hop_seconds:
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


class AsyncSpectrumAnalyzer(AnalysisWorker):
    """At most one active analysis and one latest queued URI."""

    def _load(self, uri, cache_dir):
        return load_cache(uri, cache_dir)

    def _analyze(self, uri, register_process):
        return analyze_uri(uri, register_process=register_process)

    def _available(self):
        return ffmpeg_available() and ANALYZER_BIN.exists()

    def _path(self, cache_dir, uri):
        return cache_path(cache_dir, uri)

    def _write(self, path, cache):
        write_cache(path, cache)

    def _prune(self, cache_dir):
        prune_cache(cache_dir)


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
        if path.stat().st_size > MAX_CACHE_ENTRY_BYTES:
            return None
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
    write_bounded_json(path, payload, MAX_CACHE_ENTRY_BYTES)


def prune_cache(cache_dir: Path, max_files: int = MAX_CACHE_FILES, max_age_seconds: int = MAX_CACHE_AGE_SECONDS, max_total_bytes: int = MAX_CACHE_TOTAL_BYTES) -> None:
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


def _kill_and_reap(process, drain_output=False):
    if process is None:
        return
    try:
        if process.poll() is None:
            signal_process_group(process, signal.SIGKILL)
    except OSError:
        pass
    try:
        if drain_output:
            process.communicate(timeout=1)
        else:
            process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        signal_process_group(process, signal.SIGKILL)
        try:
            process.wait(timeout=1)
        except (subprocess.TimeoutExpired, OSError):
            pass
    except OSError:
        pass


def analyze_uri(uri: str, register_process=None) -> SpectrumCache:
    # ffmpeg's mono float32 output is capped by both duration and decoded bytes.
    ffmpeg_cmd = [
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
    analyzer_cmd = [
        str(ANALYZER_BIN),
        "--sample-rate",
        str(SAMPLE_RATE),
        "--window",
        str(BLOCK_SIZE),
        "--hop",
        str(HOP_SIZE),
        "--max-samples",
        str(MAX_ANALYSIS_PCM_BYTES // 4),
        "--attack",
        "0.68",
        "--release",
        "0.14",
    ]
    ffmpeg = subprocess.Popen(
        ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True
    )
    analyzer = None
    out_bytes = b""
    analyzer_err_bytes = b""
    ffmpeg_rc = -1
    try:
        if register_process:
            register_process(ffmpeg)
        analyzer = subprocess.Popen(
            analyzer_cmd, stdin=ffmpeg.stdout, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True,
        )
        if register_process:
            register_process(analyzer)
        assert ffmpeg.stdout is not None
        ffmpeg.stdout.close()
        out_bytes, analyzer_err_bytes = analyzer.communicate(timeout=120)
        ffmpeg_rc = ffmpeg.wait(timeout=10)
    except subprocess.TimeoutExpired:
        raise RuntimeError("spectrum analysis timed out") from None
    finally:
        if ffmpeg.stdout is not None:
            ffmpeg.stdout.close()
        _kill_and_reap(analyzer, drain_output=True)
        _kill_and_reap(ffmpeg)
    out = out_bytes.decode("utf-8", errors="replace")
    analyzer_err = analyzer_err_bytes.decode("utf-8", errors="replace")
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
