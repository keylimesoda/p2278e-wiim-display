from __future__ import annotations

import json
from pathlib import Path


def write_bounded_json(path: Path, payload: dict, max_bytes: int) -> bool:
    """Atomically cache JSON only when it fits, without writing past the limit."""
    tmp = path.with_suffix(".tmp")
    size = 0
    encoder = json.JSONEncoder(separators=(",", ":"))
    try:
        with tmp.open("wb") as stream:
            tmp.chmod(0o600)
            for fragment in encoder.iterencode(payload):
                chunk = fragment.encode("utf-8")
                if size + len(chunk) > max_bytes:
                    return False
                stream.write(chunk)
                size += len(chunk)
        tmp.replace(path)
        return True
    finally:
        tmp.unlink(missing_ok=True)
