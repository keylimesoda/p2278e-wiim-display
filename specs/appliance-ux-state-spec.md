# P2278E Appliance UX State Specification

> **STATUS: SHIPPED.** This document describes the behavior that is actually
> implemented and tested in `pi-app/`, not an aspirational target. It replaces
> the earlier elaborate seven-state target spec. The runtime presentation state
> machine has exactly **four** user-facing states; everything else is a modifier
> or internal behavior.

**Artifact:** Canonical presentation-state and fault-handling spec for the
Adafruit P2278E WiiM now-playing display.
**Panel:** 64×32 RGB LED matrix, viewed ambiently at distance. Left 32×32 =
album art. Right 32×32 = title lane, progress rail, and audio meter lane.
**Runtime:** `scripts/display_live.py` (single process, systemd `Type=notify`,
`WatchdogSec=30`, `Restart=always`) driving a pure reducer in
`src/p2278e_display/appliance.py`, with `render.py` rasterizing, `wiim.py`
(SOAP/SSDP) fetching, `art_cache.py` caching art, `audio_spectrum.py` /
`audio_levels.py` analyzing, `health.py` writing telemetry, and
`system_health.py` sampling power/thermal.
**Audience:** implementers and reviewers. Every state, timing, and transition
below is buildable and verified in code.

---

## 1. Design principles

The device is a **calm ambient appliance**, not an error console.

- **P1 — Honest last-known-good persists.** Once a track is presented, its art,
  title, and progress remain on-panel through transient fetch failures. Content
  is cleared only on positive evidence it is wrong (new track, confirmed
  stop/no-media, or grace-expired staleness) — never merely because one fetch
  failed.
- **P2 — Never block the render loop.** All I/O (SOAP fetch, art download, audio
  analysis) runs off-thread. The render loop always produces a frame.
- **P3 — Degradation is a subtle overlay, not a state takeover.** Faults raise
  orthogonal boolean modifiers that decorate the current state. No new
  top-level screen is minted per exception.
- **P4 — Album art is never cropped.** Square art is shown as-is; malformed
  non-square art is letterboxed to square (`fit_square_without_cropping`).
  Missing/failed art → a deterministic generative fallback tile, never blank.
- **P5 — Technical detail is off-panel.** No error prose on the matrix. Error
  names, retry counts, and undervoltage flags live in `health.json` and
  journald only. The panel speaks in ambient cues (stale dot, status word).

---

## 2. The four runtime states

The user-facing enum is a small closed set (`appliance.UserState`):

| State | Meaning | Screen |
|---|---|---|
| `STARTING` | Process launched, no first observation yet | Fallback tile + "Connecting" |
| `NOW_PLAYING` | A track is owned and presented | Album art + title + progress + meter |
| `IDLE` | Playback confirmed stopped / no media | Fallback tile + "Idle" |
| `SEARCHING` | Source lost or never connected | Fallback tile + "Searching" |

**Modifiers** (booleans on `ApplianceState`, never new states): `paused`,
`stale`, `transitioning`, `art_degraded`, `analysis_degraded`,
`use_fallback_art`. These decorate `NOW_PLAYING` (and, for `stale`, any
retained content). Pause, mid-track transitions, and art/analysis degradation
are modifiers or internal behavior — not separate state nodes.

**Outside the presentation machine:** systemd `Restart=always` and the
`WatchdogSec=30` recovery loop are process-lifecycle mechanisms, not UX states.
A crash and restart re-enters `STARTING` from the top; it is drawn as an outer
recovery loop, not a machine state.

---

## 3. Exact timings

Defined in `appliance.py` and reloadable where noted (§9):

| Constant | Value | Meaning |
|---|---|---|
| `STARTUP_CONNECT_TIMEOUT_S` | **18 s** | No first observation by here → `SEARCHING` |
| `STALE_AFTER_S` | **12 s** | Continuous fetch failure → content marked `stale` |
| `FAULT_SEARCHING_AFTER_S` | **45 s** | Continuous fetch failure → `SEARCHING` (source lost) |
| `IDLE_CONFIRM_POLLS` | **2** | Consecutive stopped-like polls required to enter `IDLE` |

The 18 s start budget doubles as the metadata fetch retry ceiling in
`display_live.py` (`retry_interval = min(18.0, refresh * 2**min(errors,3))`).

---

## 4. Transitions

The reducer `reduce(state, obs)` is pure: `(state, observation) → new state`. An
`Observation` is either `ok` (with a normalized `transport` and metadata) or a
`failure` (poll/fetch itself failed).

**From `STARTING`:**
- First playing-like observation → `NOW_PLAYING` (new generation owner).
- First stopped-like observation → holds; needs 2 to reach `IDLE`.
- No successful observation for 18 s (failures or unknown transport) →
  `SEARCHING`.

**From `NOW_PLAYING`:**
- New stable track identity → `NOW_PLAYING` with bumped generation; brief
  `transitioning` modifier if a prior track existed.
- `PAUSED` transport → `paused` modifier (still `NOW_PLAYING`).
- Two consecutive stopped/no-media polls → `IDLE`.
- Continuous fetch failure ≥ 12 s → `stale` modifier (content retained).
- Continuous fetch failure ≥ 45 s → `SEARCHING` (content cleared).

**From `IDLE`:**
- Any playing-like observation → `NOW_PLAYING` immediately (new generation).

**From `SEARCHING`:**
- Any successful playing-like observation → `NOW_PLAYING`.
- A confirmed stop → `IDLE`.

**Idle debounce:** a single stopped poll never blanks the screen; prior visible
content is preserved until the second consecutive stop confirms `IDLE`. A
`play` between two stops resets the counter.

---

## 5. Track identity and generation ownership

- **Stable identity:** `track_id` is a SHA1 of normalized
  `title / artist / album / duration` only. Album-art and media URIs — which
  carry expiring query tokens — are **never** part of identity. A URL token
  rotation on the same song does **not** change identity and does **not** reset
  the spectrum or level meter.
- **Generation ownership:** every ownership change (new identity, or entering a
  transition) increments `generation`. Async art and analysis results are
  tagged with `(generation, track_id)` and applied only when **both** still
  match the current track (`result_is_current`). Stale results from a
  superseded track are discarded.

---

## 6. NOW_PLAYING presentation and modifiers

- **New-track art:** on ownership change, `use_fallback_art` is set true until
  the owned art fetch lands, so a new track never briefly shows the previous
  cover. Art is never cropped; the resize pipeline is **linear-light area
  resize to 64 → UnsharpMask sharpen at 64 → linear-light area resize to 32**
  (`album_tile`), cached one tile per cover.
- **Progress:** rendered only when duration is known. Unknown duration
  (`UNKNOWN_DURATION = -1`) hides the progress rail entirely — no fake bar.
- **Pause:** visually distinct. Paused keeps the real album art and a **frozen,
  amber** progress fill (vs live cyan when playing).
- **Meter / static floor:** the live meter draws only when analysis is
  available, the track is playing, and levels exist. Otherwise a **calm static
  dim floor** row is drawn — never an animated placeholder. `spectrum` mode adds
  peak-hold dots; `levels` mode is solid columns.
- **Stale cue:** past the 12 s stale threshold, a single amber pixel pulses in
  the top-right corner (`STALE_PULSE_PERIOD=30`, on for 8 frames). Quiet, not a
  shimmer. No technical prose ever appears.
- **`analysis_degraded`:** reported in health telemetry when a playing track has
  no live analysis; it does not change the state, only the meter falls back to
  the static floor.

---

## 7. Fault handling

- **Honest retention:** a failed poll preserves prior honest content. There is
  **no** technical-error screen and no exception name painted as a "track."
- **Escalation ladder:** transient failure (retain) → `stale` at 12 s → clear to
  `SEARCHING` at 45 s. A single successful observation clears the fault flags
  and resets the failure streak.
- **Resilient discovery:** `fetch_resilient` retries the known URL, then
  re-discovers the WiiM by friendly name, then re-raises the original error if
  both fail.

---

## 8. Health, power, and the watchdog

- **Atomic health:** `HealthWriter` writes `run/health.json` via
  temp-file + `os.replace` and **never raises into the render loop**; OSErrors
  (full/read-only disk) are caught and rate-limited. `read_health` tolerates
  missing/corrupt/additive fields.
- **Power/thermal semantics (`system_health.py`):** `vcgencmd get_throttled`
  bits are split into **current** (bits 0/2/3) and **sticky/history**
  (bits 16/18/19). `power_warn` is raised only when **current** undervoltage
  (bit 0) persists for ≥3 consecutive samples **and** past a 45 s startup grace.
  Sticky boot-time flags (e.g. `0x50005`) are forensic-only and never raise a
  warning on their own. Any sensor outage is nonfatal and resets the streak.
- **Watchdog:** systemd `Type=notify` with `WatchdogSec=30`. The `WATCHDOG=1`
  ping is emitted **only after a frame is actually produced** (tied to frame
  progress), so a wedged render loop is caught and the unit is restarted. This
  restart is the outer recovery loop, outside the presentation state machine.

---

## 9. Config reload

`display_live.py` re-reads `config.json` on mtime change (~2 s cadence):

- **Live-reloadable:** `brightness`, `fps` (20/30 only), `stale_seconds`,
  `clear_seconds` (validated `5 ≤ stale < clear ≤ 180`), and `friendly_name`.
- **Restart-bound:** `visualization` (levels/spectrum). Switching a live
  analyzer can orphan its ffmpeg worker, so it takes effect only on restart.

---

## 10. Independent LAN admin surface

`admin.py` + `scripts/admin_server.py` run as a **separate** systemd unit
(`p2278e-admin.service`), independent of the display: a display crash never
takes down admin, and vice versa. It never imports the hardware matrix module.

- **Bind/port:** `:8787`, bind defaults to loopback in code; the shipped unit
  sets `P2278E_ADMIN_BIND=0.0.0.0` to expose on the LAN.
- **Open reads:** `GET /`, `/api/status`, `/api/preview`, `/api/diag` are open
  on the LAN.
- **Token-gated mutations:** all POST forms/APIs require a bearer admin token
  compared in constant time (`hmac.compare_digest`); the token doubles as CSRF
  protection. **No token configured → every mutation refused (fail closed).**
- **Safe controls only:** an allowlist of validated config keys (brightness,
  fps, visualization, stale_seconds, clear_seconds, idle_dim) written
  atomically, and cache clear that unlinks only regular files inside known
  cache subdirs (art/spectrum/levels) with no traversal or symlink escape.
- **No shell, no reboot, no service/command execution** anywhere. Previews are
  rendered through `render.py` (Pillow) only.

---

## 11. Testing and fault injection

Current suites (`pi-app/tests/`) cover the shipped behavior:

- `test_appliance.py` — reducer, identity, generation, timers, idle debounce.
- `test_render_states.py` — status screens, static floor, pause vs play,
  stale pulse, unknown-duration progress hiding.
- `test_render_album.py` — no-crop, 32px cached tile, brightness floor.
- `test_resilience.py` — atomic health, sticky-vs-current power semantics
  (incl. `0x50005` boot mask → no warn), art-cache soft failure.
- `test_admin.py` — token gating, allowlist, cache safety, preview isolation.
- `test_wiim.py` — DIDL parse, SSDP discovery, fail-closed.

Fault methods are **safe**: injected failure observations, fake throttled
masks, corrupt/oversized downloads. No deliberate brownout or heat stress is
ever used — `system_health.py` only reads sensors.

### Scenario coverage checklist (compact)

The exhaustive scenario matrix is retained only as a coverage checklist; it
does **not** reintroduce runtime states.

- Start: first play, first stop×2 → idle, 18 s no-connect → searching, stays
  before 18 s, unknown transport bounded.
- Identity: deterministic, whitespace/case-normalized, empty metadata,
  URL-token change same identity, different track different identity.
- Playback: pause modifier + resume, two stops → idle, mixed stop/no-media,
  stop-then-play resets counter, idle exits on play/loading.
- Transitions: track change bumps generation, first track owns generation,
  repeated TRANSITIONING bumps once, same track after idle new generation,
  same track settles transition, rapid skips supersede, no bump on same
  identity URL change.
- Faults: short failure retains, stale at 12 s, searching at 45 s, recovery
  clears stale, recovery from searching, streak resets on success.
- Async: applies when current, ignored on generation/track mismatch, success
  clears fallback art.
- Duration: unknown explicit, known flag, unknown-duration identity stable.
- Render: unknown-duration hides progress, static floor invariant across
  frames, non-playing uses static floor, short title static / long title
  scrolls, stale pulse gate, status screens distinct, paused keeps real art +
  frozen progress, playing shows meter.
- Health/power: roundtrip, missing/corrupt/additive reads, OSError never
  raises + rate-limits, atomic no-temp-left, missing vcgencmd nonfatal, sticky
  never warns, grace suppresses then warns, current-UV streak resets, thermal
  warn from current bits, realistic boot mask no warn.
- Admin: constant-time token, bearer extract, allowlist ranges, atomic config
  preserves unknown keys, rejects bad without writing, status/diag graceful,
  cache scope/counts + symlink safety, preview isolation, live token gating,
  form redirects, 404.
- WiiM/art-cache: seconds parse, DIDL unescape, discover by name, fail closed,
  valid/corrupt/oversized downloads, prune by count/age/bytes.

---

## 12. Implementation status and known limitations

- **Panel power is not directly observable.** The HAT/panel is driven from a
  separate 5 V supply; `system_health.py` reads only the Pi's own
  `vcgencmd`/thermal sensors. Panel-side brownouts are inferred from Pi
  behavior, not measured. This is an accepted appliance limitation.
- **Boot wiring caveat.** The Pi boots when either of two power plugs is
  connected; that wiring behavior is an ongoing operational concern, tracked
  outside this spec.
- **Admin unit installed.** `p2278e-admin.service` is enabled independently
  from the display and serves the maintenance surface on LAN port 8787.
