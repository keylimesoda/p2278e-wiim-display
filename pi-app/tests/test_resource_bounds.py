import io
import threading
import time

import pytest
from PIL import Image

from p2278e_display import art_cache, audio_levels, audio_spectrum, render


@pytest.mark.parametrize('module,cls', [(audio_levels, audio_levels.AsyncLevelAnalyzer), (audio_spectrum, audio_spectrum.AsyncSpectrumAnalyzer)])
def test_analyzer_supersession_backoff_and_shutdown(tmp_path, monkeypatch, module, cls):
    started = threading.Event()
    release = threading.Event()
    calls = []
    monkeypatch.setattr(module, 'ffmpeg_available', lambda: True)
    if module is audio_spectrum:
        monkeypatch.setattr(module, 'ANALYZER_BIN', tmp_path)
    def analyze(uri, register_process=None):
        calls.append(uri)
        started.set()
        release.wait(2)
        raise ValueError('failed')
    monkeypatch.setattr(module, 'analyze_uri', analyze)
    manager = cls(tmp_path)
    manager.request('a')
    assert started.wait(1)
    manager.request('b')
    manager.request('c')
    assert calls == ['a']
    worker = manager.worker
    release.set()
    deadline = time.monotonic() + 2
    while len(calls) < 2 and time.monotonic() < deadline:
        time.sleep(.01)
    assert calls == ['a', 'c']
    worker.join(2)
    manager.request('c')
    assert calls == ['a', 'c']
    manager.close()
    manager.request('d')
    assert calls == ['a', 'c']


@pytest.mark.parametrize('module', [audio_levels, audio_spectrum])
def test_audio_duration_and_cache_byte_bounds(tmp_path, monkeypatch, module):
    assert module.MAX_ANALYSIS_SECONDS > 0
    for i in range(3):
        (tmp_path / f'{i}.{ "levels" if module is audio_levels else "spectrum"}.json').write_bytes(b'x' * 20)
    module.prune_cache(tmp_path, max_total_bytes=25)
    assert sum(p.stat().st_size for p in tmp_path.iterdir()) <= 25
    path = module.cache_path(tmp_path, 'oversized')
    path.write_bytes(b'x' * (module.MAX_CACHE_ENTRY_BYTES + 1))
    assert module.load_cache('oversized', tmp_path) is None


@pytest.mark.parametrize('module', [audio_levels, audio_spectrum])
def test_cache_writer_skips_entries_that_exceed_the_disk_limit(tmp_path, monkeypatch, module):
    from p2278e_display.audio_levels import LevelCache
    from p2278e_display.audio_spectrum import SpectrumCache

    cap = 128
    monkeypatch.setattr(module, 'MAX_CACHE_ENTRY_BYTES', cap)
    path = module.cache_path(tmp_path, 'oversized-entry')
    if module is audio_levels:
        cache = LevelCache('uri', 11025, 0.01, 1024, module.MODE, [0.5] * 1000)
    else:
        cache = SpectrumCache('uri', 48000, 800 / 48000, 2048, module.MODE, module.BANDS, [[0.5] * 9 for _ in range(100)])

    module.write_cache(path, cache)

    assert not path.exists() or path.stat().st_size <= cap
    assert not path.with_suffix('.tmp').exists()


def test_oversized_art_is_rejected_and_render_falls_back(tmp_path):
    buf = io.BytesIO()
    Image.new('RGB', (4097, 1)).save(buf, format='PNG')
    data = buf.getvalue()
    assert not art_cache.validate_image_bytes(data)
    path = tmp_path / 'oversized.png'
    path.write_bytes(data)
    assert render.load_album_art(str(path)).tobytes() == render.fallback_art().tobytes()


def test_long_audio_stops_at_duration_and_falls_back(tmp_path, monkeypatch):
    import wave
    import shutil
    if not shutil.which('ffmpeg'):
        pytest.skip('ffmpeg unavailable')
    path = tmp_path / 'long.wav'
    with wave.open(str(path), 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(11025)
        wav.writeframes(b'\x00\x00' * 22050)
    monkeypatch.setattr(audio_levels, 'MAX_ANALYSIS_SECONDS', 1)
    cache = audio_levels.analyze_uri(str(path))
    assert 0 < len(cache.frames) <= 61
    assert cache.levels_at_seconds(2) == ()


def test_download_is_cached_as_bounded_tile_and_old_oversized_cache_is_purged(tmp_path):
    import hashlib
    uri = 'http://example/art'
    buf = io.BytesIO()
    Image.new('RGB', (1024, 512), 'red').save(buf, format='PNG')
    class Response(io.BytesIO):
        headers = {}
    path = art_cache.cache_album_art(uri, tmp_path, opener=lambda *a, **k: Response(buf.getvalue()))
    with Image.open(path) as image:
        assert image.size == (256, 128)
    old = tmp_path / (hashlib.sha256(uri.encode()).hexdigest()[:24] + '.jpg')
    oversized = io.BytesIO()
    Image.new('RGB', (4097, 1)).save(oversized, format='PNG')
    old.write_bytes(oversized.getvalue())
    assert art_cache.cache_album_art(uri, tmp_path, opener=lambda *a, **k: Response(b'bad')) == ''
    assert not old.exists()


@pytest.mark.parametrize('module,cls', [(audio_levels, audio_levels.AsyncLevelAnalyzer), (audio_spectrum, audio_spectrum.AsyncSpectrumAnalyzer)])
def test_retry_cooldown_expires_and_close_discards_queued_work(tmp_path, monkeypatch, module, cls):
    from p2278e_display import analysis_worker
    now = [100.0]
    monkeypatch.setattr(analysis_worker.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(module, 'ffmpeg_available', lambda: True)
    if module is audio_spectrum:
        monkeypatch.setattr(module, 'ANALYZER_BIN', tmp_path)
    calls = []
    started = threading.Event()
    release = threading.Event()
    def analyze(uri, register_process=None):
        calls.append(uri)
        if len(calls) == 2:
            started.set()
            release.wait(2)
        raise ValueError('failed')
    monkeypatch.setattr(module, 'analyze_uri', analyze)
    manager = cls(tmp_path)
    manager.request('a')
    with manager.lock:
        worker = manager.worker
    if worker:
        worker.join(2)
    manager.request('a')
    assert calls == ['a']
    now[0] += analysis_worker.RETRY_COOLDOWN_SECONDS
    manager.request('a')
    assert started.wait(1)
    manager.request('queued')
    manager.close(wait=False)
    release.set()
    worker = manager.worker
    if worker:
        worker.join(2)



def test_close_terminates_active_analysis_process(tmp_path):
    import subprocess
    from p2278e_display.analysis_worker import AnalysisWorker

    entered = threading.Event()
    completed = threading.Event()

    class FakeProcess:
        def __init__(self):
            self.terminated = False

        def poll(self):
            return -15 if self.terminated else None

        def terminate(self):
            self.terminated = True
            completed.set()

        def kill(self):
            self.terminated = True
            completed.set()

        def wait(self, timeout=None):
            if not completed.wait(timeout):
                raise subprocess.TimeoutExpired('fake', timeout)
            return -15

    proc = FakeProcess()

    class Manager(AnalysisWorker):
        def _load(self, uri, cache_dir):
            return None

        def _available(self):
            return True

        def _path(self, cache_dir, uri):
            return cache_dir / 'unused'

        def _write(self, path, cache):
            pass

        def _prune(self, cache_dir):
            pass

        def _analyze(self, uri, register_process):
            register_process(proc)
            entered.set()
            completed.wait(5)
            return object()

    manager = Manager(tmp_path)
    manager.request('track')
    assert entered.wait(1)
    started = time.monotonic()
    manager.close()
    assert time.monotonic() - started < 1
    assert proc.terminated


def test_audio_pcm_limit_bounds_derived_decode_duration():
    assert audio_spectrum.MAX_ANALYSIS_PCM_BYTES <= 16_000_000
    assert audio_levels.MAX_ANALYSIS_PCM_BYTES <= 16_000_000
    assert audio_spectrum.MAX_FFMPEG_OUTPUT_BYTES < audio_spectrum.MAX_ANALYSIS_PCM_BYTES
    assert audio_levels.MAX_FFMPEG_OUTPUT_BYTES < audio_levels.MAX_ANALYSIS_PCM_BYTES
    assert audio_spectrum.analysis_duration_limit() == pytest.approx(
        audio_spectrum.MAX_FFMPEG_OUTPUT_BYTES / (audio_spectrum.SAMPLE_RATE * 4)
    )


def test_close_terminates_analysis_process_group(tmp_path):
    import os
    import signal
    import subprocess
    import sys
    from p2278e_display.analysis_worker import AnalysisWorker

    child_pid_file = tmp_path / 'child.pid'
    child_ready_file = tmp_path / 'child.ready'
    child_stopped_file = tmp_path / 'child.stopped'
    child_code = (
        'import pathlib,signal,sys,time; '
        f'p=pathlib.Path({str(child_stopped_file)!r}); '
        'signal.signal(signal.SIGTERM, lambda *_: (p.write_text("stopped"), sys.exit(0))); '
        f'pathlib.Path({str(child_ready_file)!r}).write_text("ready"); '
        'time.sleep(60)'
    )
    parent_code = (
        'import pathlib,subprocess,sys,time; '
        f'c=subprocess.Popen([sys.executable,"-c",{child_code!r}]); '
        f'pathlib.Path({str(child_pid_file)!r}).write_text(str(c.pid)); '
        'time.sleep(60)'
    )
    entered = threading.Event()
    children = []

    class Manager(AnalysisWorker):
        def _load(self, uri, cache_dir): return None
        def _available(self): return True
        def _path(self, cache_dir, uri): return cache_dir / 'unused'
        def _write(self, path, cache): pass
        def _prune(self, cache_dir): pass
        def _analyze(self, uri, register_process):
            proc = subprocess.Popen([sys.executable, '-c', parent_code], start_new_session=True)
            children.append(proc)
            register_process(proc)
            entered.set()
            proc.wait()
            return object()

    manager = Manager(tmp_path)
    child_pid = None
    try:
        manager.request('tree')
        assert entered.wait(2)
        deadline = time.monotonic() + 2
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert child_pid_file.exists()
        child_pid = int(child_pid_file.read_text())
        deadline = time.monotonic() + 2
        while not child_ready_file.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert child_ready_file.exists()
        manager.close()
        deadline = time.monotonic() + 1
        while not child_stopped_file.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert child_stopped_file.exists()
        assert children[0].poll() is not None
    finally:
        manager.close()
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass



def test_pcm_reader_kills_process_before_buffering_past_limit():
    import subprocess
    import sys
    from p2278e_display import audio_levels

    proc = subprocess.Popen(
        [sys.executable, '-c', 'import sys; sys.stdout.buffer.write(b"x" * 20000000)'],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        with pytest.raises(RuntimeError, match='PCM byte budget'):
            audio_levels._read_bounded_pcm(proc, limit=1_000_000, timeout=2)
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=1)


@pytest.mark.skipif(not audio_spectrum.ANALYZER_BIN.exists(), reason='native analyzer not built')
def test_native_spectrum_stops_reading_at_sample_limit():
    import json
    import struct
    import subprocess

    command = [str(audio_spectrum.ANALYZER_BIN), '--max-samples', '3']
    first_three = struct.pack('<3f', 0.1, 0.2, 0.3)
    fourth_sample = first_three + struct.pack('<f', 0.95)
    expected = subprocess.run(command, input=first_three, stdout=subprocess.PIPE, check=True)
    actual = subprocess.run(command, input=fourth_sample, stdout=subprocess.PIPE, check=True)
    assert json.loads(actual.stdout) == json.loads(expected.stdout)
