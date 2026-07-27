"""P2278E LAN maintenance admin surface (standard-library only).

This module implements the *logic* for a tiny, self-contained maintenance web
interface for the P2278E appliance. It is deliberately independent from the
primary display process: the display keeps running even if this server is dead,
and this server never imports the hardware matrix driver.

Design / security decisions (also see ``scripts/admin_server.py``):

* Standard library only for serving (``http.server``). Rendering previews reuses
  ``p2278e_display.render`` (Pillow), never ``p2278e_display.matrix`` (hardware).
* Default bind is ``127.0.0.1`` for safety. To expose on the LAN, set
  ``P2278E_ADMIN_BIND`` (or ``--bind``) to the LAN address, e.g. ``0.0.0.0`` or a
  specific interface IP. The systemd unit documents this and defaults to loopback.
* Read endpoints (``GET /``, ``/api/status``, ``/api/preview``, ``/api/diag``)
  are open on the LAN. State-changing endpoints (all POST) REQUIRE a bearer
  admin token, compared in constant time (``hmac.compare_digest``). The token is
  read from a token file (root- or user-readable) or the ``P2278E_ADMIN_TOKEN``
  env var. If no token is configured, every mutation is refused (fail closed).
* Only a fixed allowlist of config keys may be written; values are validated to
  strict ranges/sets. Unknown existing keys are preserved. Config writes are
  atomic (temp file + ``os.replace``). Cache clear only unlinks regular files
  inside known cache subdirectories (art/spectrum/levels) with no traversal.
* No shell, no arbitrary paths, no command/reboot/restart/service execution.
* Body and query size limits; security headers; no directory traversal;
  mutations require the token which also serves as CSRF protection.
"""

from __future__ import annotations

import hmac
import io
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# The package lives at pi-app/src/p2278e_display/, so parents[2] == pi-app.
PI_APP_ROOT = Path(__file__).resolve().parents[2]

# --------------------------------------------------------------------------- #
# Allowlisted config schema. Each validator returns the coerced value or raises
# ValueError. Only these keys may be mutated via POST /api/config.
# --------------------------------------------------------------------------- #

MAX_BODY_BYTES = 16 * 1024
MAX_QUERY_BYTES = 512

PREVIEW_STATES = ("starting", "now_playing", "paused", "transition", "stale", "idle", "searching")
CACHE_SCOPES = ("art", "spectrum", "levels")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
}


def _v_brightness(value: Any) -> float:
    f = float(value)
    if not (0.10 <= f <= 1.0):
        raise ValueError("brightness must be between 0.10 and 1.0")
    return round(f, 3)


def _v_fps(value: Any) -> int:
    i = int(value)
    if i not in (20, 30):
        raise ValueError("fps must be one of {20, 30}")
    return i


def _v_visualization(value: Any) -> str:
    s = str(value)
    if s not in ("levels", "spectrum"):
        raise ValueError("visualization must be one of {levels, spectrum}")
    return s


def _v_stale_seconds(value: Any) -> int:
    i = int(value)
    if not (5 <= i <= 60):
        raise ValueError("stale_seconds must be between 5 and 60")
    return i


def _v_clear_seconds(value: Any) -> int:
    i = int(value)
    if not (20 <= i <= 180):
        raise ValueError("clear_seconds must be between 20 and 180")
    return i


def _v_idle_dim(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    raise ValueError("idle_dim must be a boolean")


def _v_no_info_timeout(value: Any) -> int:
    i = int(value)
    if not (60 <= i <= 24 * 60 * 60):
        raise ValueError("no_info_timeout_seconds must be between 60 and 86400")
    return i


CONFIG_VALIDATORS: dict[str, Callable[[Any], Any]] = {
    "brightness": _v_brightness,
    "fps": _v_fps,
    "visualization": _v_visualization,
    "stale_seconds": _v_stale_seconds,
    "clear_seconds": _v_clear_seconds,
    "idle_dim": _v_idle_dim,
    "no_info_timeout_seconds": _v_no_info_timeout,
}


class ValidationError(ValueError):
    """Raised for a client-supplied validation failure (maps to HTTP 400/422)."""


# --------------------------------------------------------------------------- #
# Configuration container. Paths are injected so tests can use temp dirs.
# --------------------------------------------------------------------------- #


@dataclass
class AdminConfig:
    root: Path = PI_APP_ROOT
    config_path: Path | None = None
    health_path: Path | None = None
    cache_root: Path | None = None
    token: str | None = None
    started_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        if self.config_path is None:
            self.config_path = self.root / "config.json"
        if self.health_path is None:
            self.health_path = self.root / "run" / "health.json"
        if self.cache_root is None:
            self.cache_root = self.root / "cache"
        self.config_path = Path(self.config_path)
        self.health_path = Path(self.health_path)
        self.cache_root = Path(self.cache_root)

    def cache_dir(self, scope: str) -> Path:
        if scope not in CACHE_SCOPES:
            raise ValidationError(f"unknown cache scope: {scope!r}")
        return self.cache_root / scope


# --------------------------------------------------------------------------- #
# Token handling.
# --------------------------------------------------------------------------- #


def load_token(token_file: str | os.PathLike[str] | None = None,
               env: dict[str, str] | None = None) -> str | None:
    """Resolve the admin token from a file or env var; None if unconfigured."""
    env = env if env is not None else os.environ
    if token_file:
        try:
            text = Path(token_file).read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            pass
    value = env.get("P2278E_ADMIN_TOKEN", "").strip()
    return value or None


def token_matches(configured: str | None, provided: str | None) -> bool:
    """Constant-time bearer token check. Fails closed when unconfigured."""
    if not configured or not provided:
        return False
    return hmac.compare_digest(configured.encode("utf-8"),
                               provided.encode("utf-8"))


def extract_bearer(auth_header: str | None) -> str | None:
    if not auth_header:
        return None
    parts = auth_header.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


# --------------------------------------------------------------------------- #
# Safe JSON reads.
# --------------------------------------------------------------------------- #


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_config(cfg: AdminConfig) -> dict[str, Any]:
    return _read_json(cfg.config_path)


def read_health(cfg: AdminConfig) -> dict[str, Any]:
    return _read_json(cfg.health_path)


# --------------------------------------------------------------------------- #
# Status / diagnostics.
# --------------------------------------------------------------------------- #


def _dir_usage(path: Path) -> dict[str, Any]:
    files = 0
    total = 0
    try:
        for entry in path.iterdir():
            if entry.is_file():
                files += 1
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
    except OSError:
        return {"exists": False, "files": 0, "bytes": 0}
    return {"exists": True, "files": files, "bytes": total}


def build_status(cfg: AdminConfig) -> dict[str, Any]:
    """Assemble the appliance status payload. Graceful when files/fields absent."""
    health = read_health(cfg)
    config = read_config(cfg)

    updated_at = health.get("updated_at")
    health_age = None
    if isinstance(updated_at, (int, float)):
        health_age = round(max(0.0, time.time() - float(updated_at)), 1)

    errors = health.get("consecutive_fetch_errors")

    cache_usage = {scope: _dir_usage(cfg.cache_dir(scope)) for scope in CACHE_SCOPES}

    disk = None
    try:
        st = os.statvfs(cfg.root)
        disk = {
            "total_bytes": st.f_blocks * st.f_frsize,
            "free_bytes": st.f_bavail * st.f_frsize,
        }
    except (OSError, AttributeError):
        disk = None

    return {
        "state": health.get("canonical_state") or health.get("wiim_state"),
        "track": {
            "title": health.get("title"),
            "artist": health.get("artist"),
        },
        "endpoint": config.get("wiim_base_url"),
        "friendly_name": config.get("friendly_name"),
        "fps": health.get("estimated_fps"),
        "health_updated_at": updated_at,
        "health_age_seconds": health_age,
        "health_present": bool(health),
        "admin_uptime_seconds": round(time.time() - cfg.started_at, 1),
        "errors": errors,
        "flags": {
            "idle_dim": config.get("idle_dim"),
            "visualization": config.get("visualization"),
            "sleeping_no_info": health.get("sleeping_no_info"),
        },
        "config": {k: config.get(k) for k in CONFIG_VALIDATORS if k in config},
        "cache_usage": cache_usage,
        "disk_usage": disk,
        "implementation_status": build_implementation_status(cfg),
    }


def build_implementation_status(cfg: AdminConfig) -> dict[str, Any]:
    """Report which appliance pieces are present on disk (no side effects)."""
    return {
        "config_present": cfg.config_path.exists(),
        "health_present": cfg.health_path.exists(),
        "cache_root_present": cfg.cache_root.exists(),
        "admin_token_configured": bool(cfg.token),
    }


def build_diagnostics(cfg: AdminConfig) -> dict[str, Any]:
    """Structured diagnostics: JSON data only, never raw logs or secrets."""
    health = read_health(cfg)
    return {
        "health_present": bool(health),
        "health_age_seconds": build_status(cfg)["health_age_seconds"],
        "estimated_fps": health.get("estimated_fps"),
        "consecutive_fetch_errors": health.get("consecutive_fetch_errors"),
        "state": health.get("wiim_state"),
        "cache_usage": {s: _dir_usage(cfg.cache_dir(s)) for s in CACHE_SCOPES},
        "config_keys_present": sorted(read_config(cfg).keys()),
        "admin_uptime_seconds": round(time.time() - cfg.started_at, 1),
    }


# --------------------------------------------------------------------------- #
# Config mutation (validated allowlist, atomic, preserve unknown keys).
# --------------------------------------------------------------------------- #


def validate_config_update(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValidationError("config body must be a JSON object")
    if not payload:
        raise ValidationError("no config keys provided")
    unknown = [k for k in payload if k not in CONFIG_VALIDATORS]
    if unknown:
        raise ValidationError(f"unsupported config keys: {sorted(unknown)}")
    validated: dict[str, Any] = {}
    for key, value in payload.items():
        try:
            validated[key] = CONFIG_VALIDATORS[key](value)
        except (ValueError, TypeError) as exc:
            raise ValidationError(f"{key}: {exc}") from exc
    return validated


def apply_config_update(cfg: AdminConfig, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate, merge into existing config (preserving unknown keys), write atomically."""
    validated = validate_config_update(payload)
    current = read_config(cfg)  # preserves unknown keys
    merged = dict(current)
    merged.update(validated)
    _atomic_write_json(cfg.config_path, merged)
    return {"updated": validated, "config": merged}


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Cache clear (safe unlink within a known dir; no traversal).
# --------------------------------------------------------------------------- #


def clear_cache(cfg: AdminConfig, scope: str) -> dict[str, Any]:
    """Delete regular files directly inside cfg.cache_root/<scope>. No recursion,
    no traversal, no symlink following outside the dir."""
    if scope not in CACHE_SCOPES:
        raise ValidationError(f"scope must be one of {list(CACHE_SCOPES)}")
    directory = cfg.cache_dir(scope).resolve()
    root = cfg.cache_root.resolve()
    # Defense in depth: the resolved dir must live under the cache root.
    if root not in directory.parents and directory != root:
        raise ValidationError("cache scope escapes cache root")

    removed = 0
    freed = 0
    if directory.exists():
        for entry in directory.iterdir():
            # Only unlink plain files whose real parent is exactly this dir.
            try:
                if not entry.is_file() or entry.is_symlink():
                    continue
                real = entry.resolve()
                if real.parent != directory:
                    continue
                size = entry.stat().st_size
                entry.unlink()
                removed += 1
                freed += size
            except OSError:
                continue
    return {"scope": scope, "removed": removed, "freed_bytes": freed}


# --------------------------------------------------------------------------- #
# Preview PNG (render.py only; never imports matrix hardware module).
# --------------------------------------------------------------------------- #


def _now_playing_for_state(state: str):
    # Imported lazily so status/config/cache paths work without Pillow.
    from p2278e_display.render import NowPlaying

    if state not in PREVIEW_STATES:
        raise ValidationError(
            f"state must be one of {list(PREVIEW_STATES)}"
        )
    common = dict(source="WiiM", visualization="levels")
    if state == "starting":
        return NowPlaying(title="Connecting", artist="WiiM", state="starting",
                          progress=0.0, **common)
    if state == "now_playing":
        return NowPlaying(title="Now Playing", artist="Example Artist", state="play",
                          progress=0.42, **common)
    if state == "paused":
        return NowPlaying(title="Now Playing", artist="Example Artist", state="pause",
                          progress=0.42, **common)
    if state == "transition":
        return NowPlaying(title="Changing track", artist="", state="transition",
                          progress=0.0, analysis_available=False, **common)
    if state == "stale":
        return NowPlaying(title="Now Playing", artist="Example Artist", state="play",
                          progress=0.42, stale=True, **common)
    if state == "idle":
        return NowPlaying(title="Idle", artist="", state="idle",
                          progress=0.0, **common)
    # searching
    return NowPlaying(title="Searching", artist="WiiM", state="searching",
                      progress=0.0, **common)


def render_preview_png(state: str, brightness: float = 0.60,
                       scale: int = 8) -> bytes:
    """Render a preview PNG for a named appliance state using render.py only.

    Returns PNG bytes. Raises ValidationError for an unknown state. Uses
    nearest-neighbor upscaling so the 64x32 matrix art is visible in a browser.
    """
    from p2278e_display.render import render_now_playing, WIDTH, HEIGHT
    from PIL import Image

    now = _now_playing_for_state(state)
    img = render_now_playing(now, brightness=max(0.10, min(1.0, brightness)))
    scale = max(1, min(16, int(scale)))
    if scale > 1:
        img = img.resize((WIDTH * scale, HEIGHT * scale), Image.Resampling.NEAREST)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Minimal, calm, self-contained HTML page (no JS framework, no external assets).
# --------------------------------------------------------------------------- #


def render_index_html(cfg: AdminConfig) -> str:
    status = build_status(cfg)
    track = status.get("track") or {}

    def esc(v: Any) -> str:
        s = "" if v is None else str(v)
        return (s.replace("&", "&amp;").replace("<", "&lt;")
                 .replace(">", "&gt;").replace('"', "&quot;"))

    rows = [
        ("State", status.get("state")),
        ("Track", f"{track.get('title') or '—'} — {track.get('artist') or ''}".strip(" —")),
        ("Endpoint", status.get("endpoint")),
        ("FPS", status.get("fps")),
        ("Health age (s)", status.get("health_age_seconds")),
        ("Fetch errors", status.get("errors")),
        ("Visualization", (status.get("flags") or {}).get("visualization")),
        ("Idle dim", (status.get("flags") or {}).get("idle_dim")),
        ("Admin uptime (s)", status.get("admin_uptime_seconds")),
    ]
    status_rows = "\n".join(
        f"      <tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>" for k, v in rows
    )

    previews = "\n".join(
        f'      <figure><img alt="{esc(s)} preview" '
        f'src="/api/preview?state={esc(s)}"><figcaption>{esc(s)}</figcaption></figure>'
        for s in PREVIEW_STATES
    )

    impl = status.get("implementation_status") or {}
    token_note = ("Admin token configured — mutations enabled with bearer token."
                  if impl.get("admin_token_configured")
                  else "No admin token configured — all changes are refused (read-only).")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>P2278E Maintenance</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin: 0; background:#0e1113; color:#c9d1d3;
         font: 14px/1.5 system-ui, sans-serif; }}
  main {{ max-width: 760px; margin: 0 auto; padding: 20px 16px 48px; }}
  h1 {{ font-size: 18px; font-weight: 600; margin: 0 0 4px; }}
  p.sub {{ margin: 0 0 20px; color:#7d878b; }}
  section {{ background:#151a1d; border:1px solid #222a2e; border-radius:10px;
            padding:14px 16px; margin-bottom:16px; }}
  h2 {{ font-size: 13px; text-transform: uppercase; letter-spacing:.05em;
       color:#8b969b; margin:0 0 10px; }}
  table {{ width:100%; border-collapse: collapse; }}
  th {{ text-align:left; color:#7d878b; font-weight:500; padding:3px 8px 3px 0;
       white-space:nowrap; width:40%; }}
  td {{ padding:3px 0; color:#dfe6e8; }}
  .previews {{ display:flex; flex-wrap:wrap; gap:12px; }}
  figure {{ margin:0; text-align:center; }}
  figure img {{ image-rendering: pixelated; border:1px solid #222a2e;
               border-radius:6px; width:192px; height:96px; }}
  figcaption {{ color:#7d878b; font-size:12px; margin-top:4px; }}
  .note {{ color:#8b969b; font-size:12px; }}
  form {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }}
  label {{ color:#8b969b; font-size:12px; }}
  input,select,button {{ width:100%; margin-top:3px; padding:8px; color:#dfe6e8;
    background:#0b0e10; border:1px solid #2b353a; border-radius:5px; }}
  button {{ cursor:pointer; color:#a7d3c7; font-weight:600; }}
  .full {{ grid-column:1/-1; }}
  code {{ background:#0b0e10; padding:1px 5px; border-radius:4px; color:#a7d3c7; }}
</style>
</head>
<body>
<main>
  <h1>P2278E Maintenance</h1>
  <p class="sub">LAN-only appliance admin surface — independent of the display process.</p>

  <section>
    <h2>Status</h2>
    <table>
{status_rows}
    </table>
  </section>

  <section>
    <h2>State previews</h2>
    <div class="previews">
{previews}
    </div>
  </section>

  <section>
    <h2>Control</h2>
    <p class="note">{esc(token_note)} Changes are atomic. Visualization takes effect after the display restarts; brightness and FPS reload live.</p>
    <form method="post" action="/form/config">
      <label>Admin token<input type="password" name="token" autocomplete="current-password" required></label>
      <label>Brightness<input type="number" name="brightness" min="0.10" max="1.0" step="0.05" value="{esc((status.get('config') or {}).get('brightness') or 0.6)}"></label>
      <label>FPS<select name="fps"><option>30</option><option>20</option></select></label>
      <label>Visualization<select name="visualization"><option>spectrum</option><option>levels</option></select></label>
      <label>Stale seconds<input type="number" name="stale_seconds" min="5" max="60" value="{esc((status.get('config') or {}).get('stale_seconds') or 12)}"></label>
      <label>Clear seconds<input type="number" name="clear_seconds" min="20" max="180" value="{esc((status.get('config') or {}).get('clear_seconds') or 45)}"></label>
      <label>No-info sleep (seconds)<input type="number" name="no_info_timeout_seconds" min="60" max="86400" value="{esc((status.get('config') or {}).get('no_info_timeout_seconds') or 900)}"></label>
      <button class="full" type="submit">Apply safe settings</button>
    </form>
    <form method="post" action="/form/cache" style="margin-top:12px">
      <label>Admin token<input type="password" name="token" required></label>
      <label>Cache<select name="scope"><option>art</option><option>spectrum</option><option>levels</option></select></label>
      <button class="full" type="submit">Clear selected cache</button>
    </form>
  </section>
</main>
</body>
</html>
"""
