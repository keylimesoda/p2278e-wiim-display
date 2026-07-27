"""Album-art cache with safe download, validation, atomic write, and pruning.

Hardening over the naive version:
- Verified TLS by default. The previous implementation used
  ``ssl._create_unverified_context()`` which disabled certificate checking.
  We prefer verified TLS (the default context) and only fall back to unverified
  if the caller explicitly opts in via ``allow_insecure_tls=True`` -- because
  album-art hosts (WiiM, streaming CDNs) normally present valid certs, and
  silently bypassing verification is a real MITM risk on a home network.
- Oversized payloads are rejected safely: we check Content-Length when present
  and also stream in bounded chunks with a hard cap so a lying/absent header
  cannot exhaust memory or the SD card.
- Downloaded bytes are validated as a decodable image before being cached.
- Atomic write via temp-file + replace; a bad temp/cache file is removed.
- Bounded prune by age, count, and total bytes.
"""

from __future__ import annotations

import hashlib
import io
import os
import ssl
import time
from pathlib import Path
from typing import Any, Callable, List, Optional
from urllib.request import Request, urlopen

try:  # Pillow is already a dependency of the renderer.
    from PIL import Image
except Exception:  # pragma: no cover - Pillow should be present
    Image = None  # type: ignore[assignment]

__all__ = ["cache_album_art", "prune_cache", "validate_image_bytes"]

DEFAULT_MAX_BYTES = 5_000_000
_CHUNK = 64 * 1024


class ArtCacheError(Exception):
    """Raised for a caller-recoverable cache failure (bad/oversized image)."""


def validate_image_bytes(data: bytes) -> bool:
    """Return True if ``data`` decodes as an image via Pillow."""
    if not data:
        return False
    if Image is None:  # pragma: no cover
        # No Pillow: fall back to a minimal magic-number sniff.
        return data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
        return True
    except Exception:
        return False


def _download(
    uri: str,
    timeout: float,
    max_bytes: int,
    allow_insecure_tls: bool,
    opener: Optional[Callable[..., Any]] = None,
) -> bytes:
    """Download with Content-Length pre-check and a hard streaming cap.

    ``opener`` is injectable for tests (defaults to urllib ``urlopen``).
    """
    if opener is None:
        if uri.startswith("https://"):
            ctx = (
                ssl._create_unverified_context()
                if allow_insecure_tls
                else ssl.create_default_context()
            )
        else:
            ctx = None
        req = Request(uri, headers={"User-Agent": "p2278e-display/0.2"})
        resp_cm = urlopen(req, timeout=timeout, context=ctx)
    else:
        resp_cm = opener(uri, timeout=timeout)

    with resp_cm as r:
        # Reject early if the server advertises an oversized body.
        length: Optional[str] = None
        try:
            headers = getattr(r, "headers", None)
            if headers is not None:
                length = headers.get("Content-Length")
        except Exception:
            length = None
        if length is not None:
            try:
                if int(length) > max_bytes:
                    raise ArtCacheError(
                        f"content-length {length} exceeds cap {max_bytes}"
                    )
            except (TypeError, ValueError):
                pass  # bad header -> rely on streaming cap below

        # Stream with a hard cap regardless of (missing/lying) header.
        buf = bytearray()
        while True:
            chunk = r.read(_CHUNK)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise ArtCacheError(
                    f"download exceeded byte cap {max_bytes}"
                )
        return bytes(buf)


def cache_album_art(
    uri: str,
    cache_dir: Path,
    timeout: float = 8.0,
    max_bytes: int = DEFAULT_MAX_BYTES,
    allow_insecure_tls: bool = False,
    opener: Optional[Callable[..., Any]] = None,
    prune: bool = True,
    max_files: int = 200,
    max_total_bytes: int = 64_000_000,
    max_age_s: float = 30 * 24 * 3600,
) -> str:
    """Return path to cached album art for ``uri``. Empty string on failure.

    Never raises for the common failure modes (network, oversized, corrupt);
    returns "" so the render loop can just skip artwork.
    """
    if not uri:
        return ""
    cache_dir = Path(cache_dir)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return ""

    path = cache_dir / (hashlib.sha256(uri.encode()).hexdigest()[:24] + ".jpg")

    # Re-validate any existing cache entry; a truncated/corrupt file is purged.
    if path.exists():
        try:
            existing = path.read_bytes()
        except OSError:
            existing = b""
        if existing and validate_image_bytes(existing):
            return str(path)
        try:
            path.unlink()
        except OSError:
            pass

    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        data = _download(uri, timeout, max_bytes, allow_insecure_tls, opener)
        if not validate_image_bytes(data):
            raise ArtCacheError("downloaded bytes are not a valid image")
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
        tmp = None  # type: ignore[assignment]
    except (ArtCacheError, OSError, ValueError, ssl.SSLError):
        # Remove any partial temp/cache artifact and fail soft.
        try:
            if tmp is not None and Path(tmp).exists():
                Path(tmp).unlink()
        except OSError:
            pass
        try:
            if path.exists() and (not path.stat().st_size):
                path.unlink()
        except OSError:
            pass
        return ""

    if prune:
        try:
            prune_cache(
                cache_dir,
                max_files=max_files,
                max_total_bytes=max_total_bytes,
                max_age_s=max_age_s,
            )
        except OSError:
            pass

    return str(path)


def prune_cache(
    cache_dir: Path,
    max_files: int = 200,
    max_total_bytes: int = 64_000_000,
    max_age_s: float = 30 * 24 * 3600,
    now: Optional[float] = None,
) -> List[str]:
    """Bounded prune by age, count, then total bytes. Returns removed paths."""
    cache_dir = Path(cache_dir)
    if not cache_dir.is_dir():
        return []
    now = time.time() if now is None else now

    entries = []
    for p in cache_dir.iterdir():
        if not p.is_file() or p.name.startswith("."):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        entries.append((p, st.st_mtime, st.st_size))

    removed: List[str] = []

    def _remove(p: Path) -> None:
        try:
            p.unlink()
            removed.append(str(p))
        except OSError:
            pass

    # 1) Age.
    survivors = []
    for p, mtime, size in entries:
        if (now - mtime) > max_age_s:
            _remove(p)
        else:
            survivors.append((p, mtime, size))

    # Newest first for count/bytes decisions.
    survivors.sort(key=lambda e: e[1], reverse=True)

    # 2) Count cap.
    if len(survivors) > max_files:
        for p, _m, _s in survivors[max_files:]:
            _remove(p)
        survivors = survivors[:max_files]

    # 3) Total byte cap (drop oldest until under cap).
    # survivors is newest-first; oldest is at the end, so pop(-1).
    total = sum(s for _p, _m, s in survivors)
    while survivors and total > max_total_bytes:
        p, _m, size = survivors.pop(-1)  # oldest
        _remove(p)
        total -= size

    return removed
