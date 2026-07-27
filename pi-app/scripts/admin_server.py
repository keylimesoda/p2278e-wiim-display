#!/usr/bin/env python3
"""P2278E LAN maintenance web server (standard library only).

Run an independent, tiny admin HTTP server for the P2278E appliance. It is a
separate process from the display: if this server crashes or is stopped, the
matrix keeps rendering. This server never imports the hardware matrix driver;
previews are produced with ``p2278e_display.render`` (Pillow) only.

Usage
-----
    python3 scripts/admin_server.py [--bind ADDR] [--port PORT]
                                    [--token-file PATH]

Environment
-----------
    P2278E_ADMIN_BIND    Bind address. Default 127.0.0.1 (loopback, safe).
                         Set to a LAN address (e.g. 0.0.0.0) to expose on LAN.
    P2278E_ADMIN_PORT    Port. Default 8787.
    P2278E_ADMIN_TOKEN   Admin bearer token (used if no token file / file empty).
    P2278E_ADMIN_TOKEN_FILE  Path to a token file (root- or user-readable).

Security summary
----------------
* Read endpoints (GET /, /api/status, /api/preview, /api/diag) are open on LAN.
* All POST endpoints REQUIRE ``Authorization: Bearer <token>`` compared in
  constant time. If no token is configured, mutations are refused (fail closed).
* Config writes only touch an allowlist of validated keys, are atomic, and
  preserve unknown keys. Cache clear only unlinks regular files inside known
  cache subdirectories. No shell / arbitrary path / command / reboot / restart /
  service execution is exposed anywhere.
* Body/query size limits and security headers are enforced on every response.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

# Make the sibling package importable when run as a script.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from p2278e_display import admin  # noqa: E402
from p2278e_display.admin import (  # noqa: E402
    AdminConfig,
    MAX_BODY_BYTES,
    MAX_QUERY_BYTES,
    SECURITY_HEADERS,
    ValidationError,
    apply_config_update,
    build_diagnostics,
    build_status,
    clear_cache,
    extract_bearer,
    load_token,
    render_index_html,
    render_preview_png,
    token_matches,
)


def make_handler(cfg: AdminConfig):
    class AdminHandler(BaseHTTPRequestHandler):
        server_version = "P2278E-Admin/1.0"
        protocol_version = "HTTP/1.1"

        # ---- helpers ---------------------------------------------------- #
        def _headers(self, status: int, content_type: str, length: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            for key, value in SECURITY_HEADERS.items():
                self.send_header(key, value)
            self.end_headers()

        def _json(self, status: int, payload) -> None:
            body = (json.dumps(payload) + "\n").encode("utf-8")
            self._headers(status, "application/json; charset=utf-8", len(body))
            if self.command != "HEAD":
                self.wfile.write(body)

        def _html(self, status: int, text: str) -> None:
            body = text.encode("utf-8")
            self._headers(status, "text/html; charset=utf-8", len(body))
            if self.command != "HEAD":
                self.wfile.write(body)

        def _png(self, data: bytes) -> None:
            self._headers(200, "image/png", len(data))
            if self.command != "HEAD":
                self.wfile.write(data)

        def _redirect_home(self) -> None:
            self.send_response(303)
            self.send_header("Location", "/")
            for key, value in SECURITY_HEADERS.items():
                self.send_header(key, value)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _require_token(self, provided=None) -> bool:
            provided = provided or extract_bearer(self.headers.get("Authorization"))
            if token_matches(cfg.token, provided):
                return True
            self._json(401, {"error": "unauthorized",
                             "detail": "valid bearer admin token required"})
            return False

        def _read_body(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length < 0:
                raise ValidationError("bad content length")
            if length > MAX_BODY_BYTES:
                raise ValidationError("request body too large")
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValidationError(f"invalid JSON body: {exc}") from exc
            if not isinstance(data, dict):
                raise ValidationError("body must be a JSON object")
            return data

        def log_message(self, fmt, *args):  # quiet by default
            return

        # ---- routing ---------------------------------------------------- #
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            parts = urlsplit(self.path)
            path = parts.path.rstrip("/") or "/"
            if len(parts.query) > MAX_QUERY_BYTES:
                self._json(414, {"error": "query too long"})
                return
            query = parse_qs(parts.query)

            try:
                if path == "/":
                    self._html(200, render_index_html(cfg))
                elif path == "/api/status":
                    self._json(200, build_status(cfg))
                elif path == "/api/diag":
                    self._json(200, build_diagnostics(cfg))
                elif path == "/api/preview":
                    state = (query.get("state", ["now_playing"])[0])
                    try:
                        brightness = float(query.get("brightness", ["0.60"])[0])
                    except (TypeError, ValueError):
                        brightness = 0.60
                    try:
                        png = render_preview_png(state, brightness=brightness)
                    except ValidationError as exc:
                        self._json(422, {"error": "invalid preview", "detail": str(exc)})
                        return
                    except Exception as exc:  # rendering unavailable
                        self._json(503, {"error": "preview unavailable",
                                         "detail": type(exc).__name__})
                        return
                    self._png(png)
                else:
                    self._json(404, {"error": "not found"})
            except BrokenPipeError:
                return

        def do_POST(self):
            parts = urlsplit(self.path)
            path = parts.path.rstrip("/") or "/"
            try:
                if path in ("/form/config", "/form/cache"):
                    length = int(self.headers.get("Content-Length", 0) or 0)
                    if length < 0 or length > MAX_BODY_BYTES:
                        raise ValidationError("request body too large")
                    form = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
                    token = (form.pop("token", [""])[0] or "").strip()
                    if not self._require_token(token):
                        return
                    if path == "/form/config":
                        payload = {key: values[0] for key, values in form.items() if values and values[0] != ""}
                        apply_config_update(cfg, payload)
                    else:
                        clear_cache(cfg, (form.get("scope") or [""])[0])
                    self._redirect_home()
                    return
                if not self._require_token():
                    return
                if path == "/api/config":
                    body = self._read_body()
                    result = apply_config_update(cfg, body)
                    self._json(200, {"ok": True, **result})
                elif path == "/api/cache/clear":
                    body = self._read_body()
                    scope = body.get("scope")
                    result = clear_cache(cfg, scope)
                    self._json(200, {"ok": True, **result})
                else:
                    self._json(404, {"error": "not found"})
            except ValidationError as exc:
                self._json(422, {"error": "validation", "detail": str(exc)})
            except BrokenPipeError:
                return
            except Exception as exc:  # never leak internals
                self._json(500, {"error": "internal", "detail": type(exc).__name__})

    return AdminHandler


def build_config_from_args(args) -> AdminConfig:
    token_file = args.token_file or os.environ.get("P2278E_ADMIN_TOKEN_FILE")
    token = load_token(token_file)
    return AdminConfig(root=admin.PI_APP_ROOT, token=token)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="P2278E LAN maintenance server")
    parser.add_argument("--bind", default=os.environ.get("P2278E_ADMIN_BIND", "127.0.0.1"),
                        help="Bind address (default 127.0.0.1; set LAN addr to expose)")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("P2278E_ADMIN_PORT", "8787")),
                        help="Port (default 8787)")
    parser.add_argument("--token-file", default=None,
                        help="Path to admin token file (root/user-readable)")
    args = parser.parse_args(argv)

    cfg = build_config_from_args(args)
    handler = make_handler(cfg)
    httpd = ThreadingHTTPServer((args.bind, args.port), handler)
    scope = "LOOPBACK only" if args.bind in ("127.0.0.1", "localhost", "::1") else "LAN-exposed"
    token_state = "token configured" if cfg.token else "NO token (read-only, mutations refused)"
    sys.stderr.write(
        f"P2278E admin serving on http://{args.bind}:{args.port} "
        f"[{scope}] [{token_state}]\n"
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
