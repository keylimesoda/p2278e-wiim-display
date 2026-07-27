# P2278E Status

Updated: 2026-07-21

**Shipped:** the simple four-state appliance is implemented and tested.

## Runtime

- State machine: pure reducer in `appliance.py` — STARTING, NOW_PLAYING, IDLE,
  SEARCHING. Pause/transition/stale/degradation are modifiers, not states.
- Entrypoint: `scripts/display_live.py` (systemd `Type=notify`,
  `WatchdogSec=30`, `Restart=always`). Watchdog ping tied to frame progress.
- Renderer: no-crop album art (linear-light 64 → sharpen → 32), Doto title lane,
  progress rail (hidden when duration unknown), static meter floor when no live
  analysis, quiet amber stale cue.
- Analyzers: swappable `spectrum` (native C++ + vendored KissFFT at
  `bin/spectrum_analyzer`) and `levels`. Python handles WiiM metadata, ffmpeg
  decode orchestration, cache, and the render loop.

## Defaults (`config.json`)

`brightness 0.60`, `fps 30`, `visualization spectrum`, `stale_seconds 12`,
`clear_seconds 45`, `idle_dim true`.

## Timings

Start/search budget 18 s · stale cue 12 s · searching clear 45 s · idle
confirmation 2 polls.

## Health & admin

- `run/health.json` written atomically every ~5 s; power block splits current
  vs sticky throttle bits.
- LAN admin at `http://<pi>:8787/` — independent unit `p2278e-admin.service`,
  open reads, token-gated mutations, safe controls only, no shell/reboot.

## Tests

`python -m pytest -q` — suites cover appliance reducer, render states, album
art, resilience (health/power/art-cache), admin, and WiiM.

## Known limitations

- Panel power is not observable; only Pi sensors are sampled.
- The Pi boots when either of two power plugs is connected — ongoing wiring
  concern.
- `p2278e-admin.service` is installed and enabled independently from the display.
