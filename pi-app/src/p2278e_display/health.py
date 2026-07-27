"""Atomic, render-loop-safe health JSON writer/reader.

Design goals:
- The writer must NEVER raise into the render loop. Any OSError (disk full,
  read-only filesystem, missing parent) is caught and rate-limited so we do not
  flood logs when the failure is persistent.
- Writes are atomic: write to a temp file in the same directory, fsync-friendly
  replace() so a reader never sees a half-written file.
- The reader tolerates a missing file, corrupt JSON, and additive/unknown
  fields (forward compatibility). It returns a plain dict.

Nothing here imports the renderer; the renderer just calls ``HealthWriter.write``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

__all__ = ["HealthWriter", "read_health", "HealthReadResult"]


class HealthWriter:
    """Atomic health JSON writer that never raises into the caller.

    Parameters
    ----------
    path:
        Destination JSON file.
    log:
        Optional callable used for diagnostics. Defaults to no-op. Errors are
        rate-limited via ``log_interval_s`` so a stuck (read-only/full) disk
        does not spam logs.
    log_interval_s:
        Minimum seconds between repeated identical error logs.
    clock:
        Injectable monotonic clock (seconds), for testing rate limiting.
    """

    def __init__(
        self,
        path: os.PathLike[str] | str,
        log: Optional[Callable[[str], None]] = None,
        log_interval_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path = Path(path)
        self._log = log or (lambda _msg: None)
        self._log_interval_s = float(log_interval_s)
        self._clock = clock
        self._last_log_at: Dict[str, float] = {}
        # Simple counters so callers/tests can assert behavior without hardware.
        self.write_ok = 0
        self.write_failed = 0

    def _rate_limited_log(self, key: str, msg: str) -> None:
        now = self._clock()
        last = self._last_log_at.get(key)
        if last is None or (now - last) >= self._log_interval_s:
            self._last_log_at[key] = now
            try:
                self._log(msg)
            except Exception:
                # Logging must never take down the render loop either.
                pass

    def write(self, payload: Dict[str, Any]) -> bool:
        """Atomically write ``payload`` as JSON. Returns True on success.

        Never raises. On failure returns False and rate-limits an error log.
        """
        tmp: Optional[Path] = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = json.dumps(payload, default=str) + "\n"
            # Unique-ish temp in same dir so replace() is atomic on same fs.
            tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(data)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    # fsync can fail on some filesystems; the replace() below is
                    # still atomic enough for our appliance purposes.
                    pass
            os.replace(tmp, self.path)
            tmp = None
            self.write_ok += 1
            return True
        except OSError as exc:
            self.write_failed += 1
            self._rate_limited_log(
                key=f"write:{type(exc).__name__}:{exc.errno}",
                msg=f"health write failed ({self.path}): {exc}",
            )
            return False
        except Exception as exc:  # pragma: no cover - defensive catch-all
            self.write_failed += 1
            self._rate_limited_log(
                key=f"write:unexpected:{type(exc).__name__}",
                msg=f"health write unexpected error ({self.path}): {exc}",
            )
            return False
        finally:
            # Clean up a stray temp file if replace() never happened.
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass


class HealthReadResult(dict):
    """Health payload plus read metadata.

    Behaves like the parsed dict (so existing consumers keep working) while
    exposing ``ok`` and ``error`` for probes that want detail.
    """

    def __init__(
        self,
        data: Optional[Dict[str, Any]] = None,
        ok: bool = False,
        error: Optional[str] = None,
    ) -> None:
        super().__init__(data or {})
        self.ok = ok
        self.error = error


def read_health(path: os.PathLike[str] | str) -> HealthReadResult:
    """Read health JSON, tolerating missing/corrupt/additive fields.

    Never raises. Unknown/extra fields are preserved. Returns a
    ``HealthReadResult`` (a dict subclass) with ``.ok``/``.error``.
    """
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return HealthReadResult(ok=False, error="missing")
    except OSError as exc:
        return HealthReadResult(ok=False, error=f"oserror:{exc}")

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        return HealthReadResult(ok=False, error=f"corrupt:{exc}")

    if not isinstance(data, dict):
        return HealthReadResult(ok=False, error="not-object")

    return HealthReadResult(data=data, ok=True, error=None)
