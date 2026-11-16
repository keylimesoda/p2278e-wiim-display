# P2278E application

The deployable Python/C++ appliance lives here. For hardware, wiring, safety,
and the complete clean-install walkthrough, start with the
[repository README](../README.md).

## Directory map

- `src/p2278e_display/appliance.py` — pure four-state reducer
- `scripts/display_live.py` — metadata/art/analyzer workers and render loop
- `src/p2278e_display/render.py` — hardware-independent 64×32 rasterizer
- `src/p2278e_display/wiim.py` — WiiM/Linkplay SSDP and UPnP/SOAP client
- `native/` + `vendor/kissfft/` — native nine-band spectrum analyzer
- `src/p2278e_display/health.py` — atomic health telemetry
- `src/p2278e_display/admin.py` + `scripts/admin_server.py` — maintenance UI
- `systemd/` — installer-rendered service templates
- `tests/` — hardware-free test suite

## Laptop development

Python 3.11+:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python scripts/render_preview.py --out output/preview.png --viz spectrum
python -m pytest -q
python -m py_compile scripts/*.py src/p2278e_display/*.py tests/*.py
./scripts/build_spectrum_analyzer.sh
```

`pytest.ini` restricts collection to `tests/`, so Pi-only display utilities are
not imported by the hardware-free suite.

## Pi installation

```bash
cp config.example.json config.json
nano config.json
sudo ./scripts/install.sh
```

The installer is idempotent, parameterized by `P2278E_USER`,
`P2278E_APP_DIR`, `P2278E_VENV_DIR`, and `P2278E_ADMIN_BIND`, and manages only
the two `p2278e-*` systemd units. `scripts/uninstall.sh` removes those units
without deleting project/configuration state by default.

## Runtime

```bash
source .venv/bin/activate
python scripts/display_live.py --duration 0
```

Flags: `--brightness`, `--fps {20,30}`, `--viz {levels,spectrum}`, `--base-url`,
and `--refresh`. Defaults come from `config.json` when flags are omitted.

Metadata polling is bounded to an 8-second overall deadline. SOAP bodies are
limited to 256 KiB, device-description bodies to 128 KiB, and SSDP retains at
most 16 description locations per pass. Description fetches share the remaining
deadline and a 16-worker global cap, so a slow unrelated UPnP device cannot block
a responsive WiiM or accumulate workers across retries. If a metadata request is
still pending at its deadline, the appliance starts aging last-known-good content
as stale/searching while the worker winds down.

Health is written atomically to `run/health.json`. Analysis caches contain only
numeric envelopes keyed by a URL hash; raw media URLs and query tokens are not
persisted. Runtime state, configuration, tokens, caches, and logs are ignored by
Git.

## Admin security model

The admin service defaults to loopback. GET endpoints are unauthenticated and
include current metadata/diagnostics; all POST mutations require a random local
token and are allowlisted. Expose it on a LAN only when you accept that read
metadata is visible to that trusted network. Never expose it to the internet.

## Hardware assumptions

- Raspberry Pi 5
- 64×32 panel, 1:16 scan, four address lines
- `AdafruitMatrixBonnet` pinout
- `RGB888Packed` colorspace
- software brightness scaling in the renderer

The matrix backend is intentionally isolated in `src/p2278e_display/matrix.py`.
