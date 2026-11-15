"""Single-worker latest-request queue shared by audio analyzers.

A running analysis is allowed to finish (its subprocess has a timeout); at most
one replacement URI is retained. Failures are cooled down for 30 seconds.
"""
import os
import signal
import threading
import time
from collections import OrderedDict

RETRY_COOLDOWN_SECONDS = 30.0
MAX_FAILED_URIS = 64


def signal_process_group(process, sig):
    """Signal a spawned process group, falling back to the direct child."""
    try:
        pid = process.pid
        if os.name == 'posix' and pid:
            os.killpg(pid, sig)
            return
    except (AttributeError, OSError):
        pass
    try:
        (process.terminate if sig == signal.SIGTERM else process.kill)()
    except (OSError, AttributeError):
        pass


class AnalysisWorker:
    def __init__(self, cache_dir, logger=None):
        self.cache_dir = cache_dir
        self.logger = logger or (lambda _msg: None)
        self.current_uri = ''
        self.current_cache = None
        self.pending_uri = ''
        self.worker = None
        self.lock = threading.Lock()
        self.closed = False
        self.failures = OrderedDict()
        self.processes = set()
        cache_dir.mkdir(parents=True, exist_ok=True)
        self._prune(cache_dir)

    def request(self, uri):
        with self.lock:
            if self.closed:
                return None
            if not uri:
                self.current_uri = self.pending_uri = ''
                self.current_cache = None
                return None
            if uri == self.current_uri:
                self.pending_uri = ''
                return self.current_cache
            if uri == self.pending_uri:
                return None
            self.current_uri = ''
            self.current_cache = None
            self.pending_uri = uri
            cached = self._load(uri, self.cache_dir)
            if cached:
                self.current_uri, self.current_cache, self.pending_uri = uri, cached, ''
                return cached
            if self.worker is not None:
                return None
            if time.monotonic() < self.failures.get(uri, 0):
                self.pending_uri = ''
                return None
            if not self._available():
                self.pending_uri = ''
                return None
            self.worker = threading.Thread(target=self._run, daemon=True, name='p2278e-audio-analyzer')
            self.worker.start()
            return None

    def _register_process(self, process):
        with self.lock:
            should_stop = self.closed
            if not should_stop:
                self.processes.add(process)
        if should_stop:
            self._stop_process(process)

    @staticmethod
    def _stop_process(process):
        try:
            if process.poll() is None:
                signal_process_group(process, signal.SIGTERM)
        except (OSError, AttributeError):
            pass
        try:
            process.wait(timeout=0.5)
        except Exception:
            signal_process_group(process, signal.SIGKILL)
            try:
                process.wait(timeout=0.5)
            except Exception:
                pass

    def _run(self):
        while True:
            with self.lock:
                uri = self.pending_uri
                if self.closed or not uri:
                    self.worker = None
                    return
                self.processes.clear()
            try:
                cache = self._analyze(uri, self._register_process)
                self._write(self._path(self.cache_dir, uri), cache)
                self._prune(self.cache_dir)
                with self.lock:
                    if not self.closed and self.pending_uri == uri:
                        self.current_uri, self.current_cache, self.pending_uri = uri, cache, ''
            except Exception as exc:
                with self.lock:
                    self.failures[uri] = time.monotonic() + RETRY_COOLDOWN_SECONDS
                    self.failures.move_to_end(uri)
                    while len(self.failures) > MAX_FAILED_URIS:
                        self.failures.popitem(last=False)
                    if self.pending_uri == uri:
                        self.pending_uri = ''
                self.logger(f'audio analysis failed {type(exc).__name__}')
            finally:
                with self.lock:
                    self.processes.clear()
            with self.lock:
                if self.pending_uri and time.monotonic() < self.failures.get(self.pending_uri, 0):
                    self.pending_uri = ''

    def close(self, wait=True):
        """Cancel active children and finish promptly during service shutdown."""
        with self.lock:
            self.closed = True
            self.pending_uri = ''
            worker = self.worker
            processes = tuple(self.processes)
        for process in processes:
            self._stop_process(process)
        if wait and worker is not None:
            worker.join(timeout=1.0)

    def levels_at(self, uri, rel_time):
        cache = self.request(uri)
        return cache.levels_at(rel_time) if cache else ()

    def levels_at_seconds(self, uri, seconds):
        cache = self.request(uri)
        return cache.levels_at_seconds(seconds) if cache else ()
