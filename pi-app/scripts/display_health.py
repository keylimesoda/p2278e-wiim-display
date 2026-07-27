#!/usr/bin/env python3
"""Appliance health probe.

Reads the render loop's health JSON via the tolerant reader and reports a
HEALTHY/UNHEALTHY line plus exit code (0/1). Freshness is judged by wall clock,
but a suspect clock (health timestamp in the future, or an absurdly large age)
is handled sensibly instead of blindly failing: if the file exists and is
recent by mtime, we downgrade a clock-skew case to a warning rather than a hard
UNHEALTHY.

Output remains compatible with the previous version:
  HEALTHY: age=.. fps=.. state=.. title=.. errors=..
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from p2278e_display.health import read_health  # noqa: E402

MAX_AGE_S = 15.0

health_path = Path(__file__).resolve().parents[1] / "run" / "health.json"


def main() -> int:
    health = read_health(health_path)
    if not health.ok:
        reason = health.error or "unknown"
        print(f"UNHEALTHY: cannot read {health_path}: {reason}")
        return 1

    now = time.time()
    updated_at = health.get("updated_at", 0)
    try:
        updated_at = float(updated_at)
    except (TypeError, ValueError):
        updated_at = 0.0

    age = now - updated_at

    # Suspect clock handling: a negative age (timestamp in the future) or an
    # absurd age can mean NTP jumped or the RTC is wrong. Fall back to file
    # mtime, which is monotonic-ish with respect to the writer.
    clock_note = ""
    if age < 0 or age > 365 * 24 * 3600:
        try:
            mtime_age = now - health_path.stat().st_mtime
        except OSError:
            mtime_age = age
        clock_note = f" (suspect clock: json-age={age:.1f}s, using mtime)"
        age = mtime_age

    if age > MAX_AGE_S:
        print(f"UNHEALTHY: render heartbeat is {age:.1f}s old{clock_note}")
        return 1

    print(
        "HEALTHY: age={:.1f}s fps={} state={} title={!r} errors={}{}".format(
            age,
            health.get("estimated_fps"),
            health.get("wiim_state"),
            health.get("title"),
            health.get("consecutive_fetch_errors"),
            clock_note,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
