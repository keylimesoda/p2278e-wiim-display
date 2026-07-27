"""Unit tests for the P2278E LAN maintenance admin surface.

Most tests exercise the pure logic in ``p2278e_display.admin`` against temp dirs
without binding a socket. A small set spins up an ephemeral loopback server to
verify routing, auth, and security headers end to end.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from p2278e_display import admin
from p2278e_display.admin import (
    AdminConfig,
    ValidationError,
    apply_config_update,
    build_diagnostics,
    build_implementation_status,
    build_status,
    clear_cache,
    extract_bearer,
    load_token,
    read_config,
    render_index_html,
    render_preview_png,
    token_matches,
    validate_config_update,
)

# admin_server lives under scripts/ (parents[1] == pi-app); load it directly.
import importlib.util
import sys

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "admin_server.py"
_spec = importlib.util.spec_from_file_location("admin_server", _SCRIPT)
admin_server = importlib.util.module_from_spec(_spec)
sys.modules["admin_server"] = admin_server
_spec.loader.exec_module(admin_server)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def cfg(tmp_path: Path) -> AdminConfig:
    (tmp_path / "run").mkdir()
    for scope in ("art", "spectrum", "levels"):
        (tmp_path / "cache" / scope).mkdir(parents=True)
    (tmp_path / "config.json").write_text(json.dumps({
        "wiim_base_url": "http://192.0.2.5:49152",
        "friendly_name": "Example Renderer",
        "brightness": 0.60,
        "visualization": "spectrum",
        "custom_unknown": "keep-me",
    }))
    (tmp_path / "run" / "health.json").write_text(json.dumps({
        "updated_at": time.time(),
        "estimated_fps": 30,
        "wiim_state": "play",
        "title": "Now Playing",
        "artist": "Example Artist",
        "consecutive_fetch_errors": 0,
    }))
    return AdminConfig(root=tmp_path, token="s3cr3t-token")


# --------------------------------------------------------------------------- #
# Token / auth
# --------------------------------------------------------------------------- #


def test_token_matches_constant_time_semantics():
    assert token_matches("abc", "abc") is True
    assert token_matches("abc", "abd") is False
    assert token_matches("", "abc") is False       # unconfigured -> fail closed
    assert token_matches("abc", None) is False
    assert token_matches(None, None) is False


def test_extract_bearer():
    assert extract_bearer("Bearer xyz") == "xyz"
    assert extract_bearer("bearer  xyz ") == "xyz"
    assert extract_bearer("Basic xyz") is None
    assert extract_bearer(None) is None


def test_load_token_prefers_file_then_env(tmp_path: Path):
    tf = tmp_path / "tok"
    tf.write_text("  filetoken\n")
    assert load_token(tf, env={}) == "filetoken"
    assert load_token(None, env={"P2278E_ADMIN_TOKEN": "envtok"}) == "envtok"
    assert load_token(None, env={}) is None
    # Empty file falls through to env.
    (tmp_path / "empty").write_text("")
    assert load_token(tmp_path / "empty", env={"P2278E_ADMIN_TOKEN": "e2"}) == "e2"


# --------------------------------------------------------------------------- #
# Config validation
# --------------------------------------------------------------------------- #


def test_validate_config_allowlist_and_ranges():
    ok = validate_config_update({
        "brightness": 0.5, "fps": 30, "visualization": "levels",
        "stale_seconds": 10, "clear_seconds": 60, "idle_dim": "true",
    })
    assert ok["brightness"] == 0.5
    assert ok["fps"] == 30
    assert ok["idle_dim"] is True

    with pytest.raises(ValidationError):
        validate_config_update({"brightness": 0.05})   # below range
    with pytest.raises(ValidationError):
        validate_config_update({"brightness": 1.5})    # above range
    with pytest.raises(ValidationError):
        validate_config_update({"fps": 25})            # not in {20,30}
    with pytest.raises(ValidationError):
        validate_config_update({"visualization": "waves"})
    with pytest.raises(ValidationError):
        validate_config_update({"stale_seconds": 4})
    with pytest.raises(ValidationError):
        validate_config_update({"clear_seconds": 200})
    with pytest.raises(ValidationError):
        validate_config_update({"unknown_key": 1})     # not allowlisted
    with pytest.raises(ValidationError):
        validate_config_update({})                     # empty


def test_apply_config_atomic_preserves_unknown_keys(cfg: AdminConfig):
    result = apply_config_update(cfg, {"brightness": 0.9, "fps": 20})
    assert result["updated"] == {"brightness": 0.9, "fps": 20}
    on_disk = read_config(cfg)
    # Mutated values written.
    assert on_disk["brightness"] == 0.9
    assert on_disk["fps"] == 20
    # Unknown/existing keys preserved.
    assert on_disk["custom_unknown"] == "keep-me"
    assert on_disk["friendly_name"] == "Example Renderer"
    # No stray temp file left behind.
    assert not (cfg.config_path.with_name(cfg.config_path.name + ".tmp")).exists()


def test_apply_config_rejects_bad_without_writing(cfg: AdminConfig):
    before = cfg.config_path.read_text()
    with pytest.raises(ValidationError):
        apply_config_update(cfg, {"brightness": 5.0})
    assert cfg.config_path.read_text() == before  # untouched on failure


# --------------------------------------------------------------------------- #
# Status / diagnostics — graceful when missing
# --------------------------------------------------------------------------- #


def test_status_reports_expected_fields(cfg: AdminConfig):
    st = build_status(cfg)
    assert st["state"] == "play"
    assert st["track"]["title"] == "Now Playing"
    assert st["endpoint"] == "http://192.0.2.5:49152"
    assert st["fps"] == 30
    assert st["errors"] == 0
    assert "cache_usage" in st and set(st["cache_usage"]) == {"art", "spectrum", "levels"}
    assert st["implementation_status"]["admin_token_configured"] is True


def test_status_graceful_when_health_missing(tmp_path: Path):
    (tmp_path / "config.json").write_text("{}")
    c = AdminConfig(root=tmp_path, token="t")
    st = build_status(c)  # no health.json, no cache dirs
    assert st["state"] is None
    assert st["health_present"] is False
    assert st["health_age_seconds"] is None
    assert st["errors"] is None
    # Cache dirs absent -> reported as not existing, not crashing.
    assert st["cache_usage"]["art"]["exists"] is False


def test_status_graceful_when_config_corrupt(tmp_path: Path):
    (tmp_path / "config.json").write_text("{not json")
    c = AdminConfig(root=tmp_path, token="t")
    st = build_status(c)
    assert st["endpoint"] is None
    assert build_implementation_status(c)["config_present"] is True


def test_diagnostics_returns_structured_json_no_secrets(cfg: AdminConfig):
    diag = build_diagnostics(cfg)
    assert diag["state"] == "play"
    assert "cache_usage" in diag
    # The admin token must never appear in diagnostics output.
    assert "s3cr3t-token" not in json.dumps(diag)


# --------------------------------------------------------------------------- #
# Cache clear — scope + path safety
# --------------------------------------------------------------------------- #


def test_cache_clear_scope_and_counts(cfg: AdminConfig):
    art = cfg.cache_dir("art")
    (art / "a.jpg").write_bytes(b"1234")
    (art / "b.jpg").write_bytes(b"56")
    (cfg.cache_dir("spectrum") / "s.bin").write_bytes(b"x")
    res = clear_cache(cfg, "art")
    assert res["removed"] == 2
    assert res["freed_bytes"] == 6
    assert list(art.iterdir()) == []
    # Other scopes untouched.
    assert (cfg.cache_dir("spectrum") / "s.bin").exists()


def test_cache_clear_rejects_unknown_scope(cfg: AdminConfig):
    with pytest.raises(ValidationError):
        clear_cache(cfg, "../../etc")
    with pytest.raises(ValidationError):
        clear_cache(cfg, "passwords")


def test_cache_clear_does_not_follow_symlink_out_of_dir(cfg: AdminConfig, tmp_path: Path):
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("do not delete")
    link = cfg.cache_dir("art") / "evil"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this platform")
    clear_cache(cfg, "art")
    # The symlink target outside the cache dir must survive.
    assert outside.exists()


# --------------------------------------------------------------------------- #
# Preview PNG (render.py only)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("state", ["starting", "now_playing", "paused", "idle", "searching"])
def test_preview_returns_png_bytes(state: str):
    data = render_preview_png(state, scale=2)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


def test_preview_rejects_unknown_state():
    with pytest.raises(ValidationError):
        render_preview_png("dancing")


def test_preview_does_not_import_matrix_module():
    # Rendering a preview must not pull in the hardware matrix driver.
    sys.modules.pop("p2278e_display.matrix", None)
    render_preview_png("now_playing", scale=1)
    assert "p2278e_display.matrix" not in sys.modules


# --------------------------------------------------------------------------- #
# HTML page
# --------------------------------------------------------------------------- #


def test_index_html_is_self_contained(cfg: AdminConfig):
    html = render_index_html(cfg)
    assert "<title>P2278E Maintenance</title>" in html
    assert "http://" not in html.split("<style>")[0]  # no external head assets
    assert "src=\"http" not in html                    # no external asset src
    assert "/api/preview?state=now_playing" in html
    # Token must never be embedded in the page.
    assert "s3cr3t-token" not in html


# --------------------------------------------------------------------------- #
# End-to-end via ephemeral loopback server (127.0.0.1 only)
# --------------------------------------------------------------------------- #


@pytest.fixture
def live_server(cfg: AdminConfig):
    handler = admin_server.make_handler(cfg)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    base = f"http://{host}:{port}"
    try:
        yield base
    finally:
        httpd.shutdown()
        httpd.server_close()


def _req(url, method="GET", data=None, token=None):
    headers = {}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def test_live_status_open_and_has_security_headers(live_server):
    status, body, headers = _req(live_server + "/api/status")
    assert status == 200
    payload = json.loads(body)
    assert payload["state"] == "play"
    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert headers.get("X-Frame-Options") == "DENY"


def test_live_post_requires_token(live_server):
    status, body, _ = _req(live_server + "/api/config", method="POST",
                           data={"brightness": 0.5})
    assert status == 401
    # With a bad token, still refused.
    status, _, _ = _req(live_server + "/api/config", method="POST",
                        data={"brightness": 0.5}, token="wrong")
    assert status == 401


def test_live_post_config_with_token(live_server, cfg):
    status, body, _ = _req(live_server + "/api/config", method="POST",
                           data={"brightness": 0.75}, token="s3cr3t-token")
    assert status == 200
    assert json.loads(body)["config"]["brightness"] == 0.75


def test_live_post_config_validation_error(live_server):
    status, body, _ = _req(live_server + "/api/config", method="POST",
                           data={"fps": 99}, token="s3cr3t-token")
    assert status == 422


def test_live_preview_endpoint_png(live_server):
    status, body, headers = _req(live_server + "/api/preview?state=paused")
    assert status == 200
    assert headers.get("Content-Type") == "image/png"
    assert body[:4] == b"\x89PNG"


def test_live_transition_and_stale_previews(live_server):
    for state in ("transition", "stale"):
        status, body, headers = _req(live_server + f"/api/preview?state={state}")
        assert status == 200
        assert headers.get("Content-Type") == "image/png"
        assert body[:4] == b"\x89PNG"


def test_live_preview_bad_state(live_server):
    status, _, _ = _req(live_server + "/api/preview?state=zzz")
    assert status == 422


def test_live_cache_clear_requires_token_then_works(live_server, cfg):
    (cfg.cache_dir("levels") / "l.bin").write_bytes(b"abc")
    status, _, _ = _req(live_server + "/api/cache/clear", method="POST",
                        data={"scope": "levels"})
    assert status == 401
    status, body, _ = _req(live_server + "/api/cache/clear", method="POST",
                           data={"scope": "levels"}, token="s3cr3t-token")
    assert status == 200
    assert json.loads(body)["removed"] == 1


def test_live_form_config_requires_token(live_server):
    req = urllib.request.Request(
        live_server + "/form/config",
        data=b"brightness=0.5&token=wrong",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        assert False, "expected HTTPError"
    except urllib.error.HTTPError as exc:
        assert exc.code == 401


def test_live_form_config_redirects_with_token(live_server, cfg):
    req = urllib.request.Request(
        live_server + "/form/config",
        data=b"brightness=0.55&fps=30&token=s3cr3t-token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
    with opener.open(req, timeout=5) as resp:
        assert resp.status == 200
    assert json.loads(cfg.config_path.read_text())["brightness"] == 0.55


def test_live_unknown_route_404(live_server):
    status, _, _ = _req(live_server + "/api/nope")
    assert status == 404
