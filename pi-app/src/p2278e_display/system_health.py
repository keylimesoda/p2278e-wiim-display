"""Power / thermal telemetry for the Pi appliance.

Safe, testable abstraction over ``vcgencmd get_throttled`` and CPU temperature.
Nothing here brings hardware near a brownout or thermal limit; it only *reads*
sensors and never stresses them.

Throttled bit semantics (Raspberry Pi ``vcgencmd get_throttled``):

  Current (live) condition bits:
    bit 0  : under-voltage detected NOW
    bit 1  : ARM frequency capped NOW
    bit 2  : currently throttled
    bit 3  : soft temperature limit active NOW

  Sticky / "has occurred since boot" history bits:
    bit 16 : under-voltage has occurred
    bit 17 : ARM frequency capping has occurred
    bit 18 : throttling has occurred
    bit 19 : soft temperature limit has occurred

Per the task: undervoltage=bit0, throttled/soft-temp=bits2/3 are the *current*
warnings we act on. Sticky bits 16/18/19 are forensic-only and MUST NOT raise a
power_warn on their own -- a brief boot-time undervoltage flag is expected and
should never trip an alert.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

__all__ = ["PowerThermalSampler", "sample"]

# --- Bit masks -------------------------------------------------------------
BIT_UNDERVOLT_NOW = 1 << 0
BIT_FREQ_CAP_NOW = 1 << 1
BIT_THROTTLED_NOW = 1 << 2
BIT_SOFT_TEMP_NOW = 1 << 3

BIT_UNDERVOLT_STICKY = 1 << 16
BIT_FREQ_CAP_STICKY = 1 << 17
BIT_THROTTLED_STICKY = 1 << 18
BIT_SOFT_TEMP_STICKY = 1 << 19

_SYSFS_TEMP = "/sys/class/thermal/thermal_zone0/temp"


# Provider signature: () -> Optional[int]  (raw throttled value) and
#                      () -> Optional[float] (temp in Celsius)
ThrottledProvider = Callable[[], Optional[int]]
TempProvider = Callable[[], Optional[float]]


def _vcgencmd_throttled() -> Optional[int]:
    """Read ``vcgencmd get_throttled`` -> int, or None if unavailable."""
    try:
        out = subprocess.run(
            ["vcgencmd", "get_throttled"],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    # Expected: "throttled=0x50005"
    txt = (out.stdout or "").strip()
    _, _, val = txt.partition("=")
    val = val.strip()
    if not val:
        return None
    try:
        return int(val, 0)
    except ValueError:
        return None


def _sysfs_temp() -> Optional[float]:
    try:
        raw = Path(_SYSFS_TEMP).read_text().strip()
        milli = int(raw)
        return round(milli / 1000.0, 1)
    except (OSError, ValueError):
        return None


def _vcgencmd_temp() -> Optional[float]:
    try:
        out = subprocess.run(
            ["vcgencmd", "measure_temp"],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    # Expected: "temp=54.0'C"
    txt = (out.stdout or "").strip()
    _, _, val = txt.partition("=")
    val = val.strip().rstrip("'C").rstrip("C").strip("'")
    try:
        return round(float(val), 1)
    except ValueError:
        return None


class PowerThermalSampler:
    """Stateful sampler that tracks *current* undervoltage over time.

    - ``power_warn`` is raised only when current undervoltage (bit0) has been
      observed for at least ``repeat_threshold`` consecutive samples AND we are
      past the ``startup_grace_s`` window. Sticky history bits never raise it.
    - ``thermal_warn`` reflects current bits 2/3 (throttled / soft temp).
    - Any sampling failure is nonfatal: fields become None/absent, no raise.
    """

    def __init__(
        self,
        throttled_provider: Optional[ThrottledProvider] = None,
        temp_provider: Optional[TempProvider] = None,
        startup_grace_s: float = 45.0,
        repeat_threshold: int = 3,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._throttled_provider = throttled_provider or _vcgencmd_throttled
        self._temp_provider = temp_provider or self._default_temp
        self.startup_grace_s = float(startup_grace_s)
        self.repeat_threshold = int(repeat_threshold)
        self._clock = clock
        self._started = clock()
        self._uv_streak = 0

    @staticmethod
    def _default_temp() -> Optional[float]:
        t = _sysfs_temp()
        if t is not None:
            return t
        return _vcgencmd_temp()

    def _elapsed(self) -> float:
        return self._clock() - self._started

    def sample(self) -> Dict[str, Any]:
        """Return a simple, JSON-serializable telemetry dict. Never raises."""
        result: Dict[str, Any] = {
            "throttled_available": False,
            "throttled_raw": None,
            "undervolt_now": False,
            "throttled_now": False,
            "soft_temp_now": False,
            # Forensic-only sticky history.
            "undervolt_sticky": False,
            "throttled_sticky": False,
            "soft_temp_sticky": False,
            "power_warn": False,
            "thermal_warn": False,
            "uv_streak": 0,
            "in_grace": self._elapsed() < self.startup_grace_s,
            "temp_c": None,
        }

        try:
            raw = self._throttled_provider()
        except Exception:
            raw = None

        if isinstance(raw, int):
            result["throttled_available"] = True
            result["throttled_raw"] = raw
            uv_now = bool(raw & BIT_UNDERVOLT_NOW)
            thr_now = bool(raw & BIT_THROTTLED_NOW)
            soft_now = bool(raw & BIT_SOFT_TEMP_NOW)
            result["undervolt_now"] = uv_now
            result["throttled_now"] = thr_now
            result["soft_temp_now"] = soft_now
            result["undervolt_sticky"] = bool(raw & BIT_UNDERVOLT_STICKY)
            result["throttled_sticky"] = bool(raw & BIT_THROTTLED_STICKY)
            result["soft_temp_sticky"] = bool(raw & BIT_SOFT_TEMP_STICKY)

            # Track consecutive *current* undervoltage only.
            if uv_now:
                self._uv_streak += 1
            else:
                self._uv_streak = 0
            result["uv_streak"] = self._uv_streak

            past_grace = self._elapsed() >= self.startup_grace_s
            result["in_grace"] = not past_grace
            if past_grace and self._uv_streak >= self.repeat_threshold:
                result["power_warn"] = True

            # thermal_warn from current bits 2/3 (does not need grace).
            result["thermal_warn"] = thr_now or soft_now
        else:
            # Sampling failed -> nonfatal; leave defaults, reset streak so a
            # sensor outage cannot fabricate a repeated-UV warning.
            self._uv_streak = 0
            result["uv_streak"] = 0

        try:
            temp = self._temp_provider()
        except Exception:
            temp = None
        if isinstance(temp, (int, float)):
            result["temp_c"] = round(float(temp), 1)

        return result


def sample(**kwargs: Any) -> Dict[str, Any]:
    """One-shot convenience sampler (no repeat/grace state persisted)."""
    return PowerThermalSampler(**kwargs).sample()
