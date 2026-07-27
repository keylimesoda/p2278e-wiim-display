#!/usr/bin/env python3
"""P2278E appliance runtime.

The runtime deliberately stays small: one pure four-state reducer, one metadata
worker, one artwork worker, one render loop, and additive health telemetry.
Network/storage failures never replace honest music content with error prose.
"""
from __future__ import annotations

import argparse
from concurrent.futures import Future, ThreadPoolExecutor
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import p2278e_display.appliance as appliance_core
from p2278e_display.appliance import (
    ApplianceState,
    AsyncResult,
    Observation,
    Transport,
    UNKNOWN_DURATION,
    UserState,
    apply_async_result,
    reduce,
    result_is_current,
    should_sleep_no_info,
)
from p2278e_display.art_cache import cache_album_art
from p2278e_display.audio_levels import AsyncLevelAnalyzer, parse_hms
from p2278e_display.audio_spectrum import AsyncSpectrumAnalyzer
from p2278e_display.health import HealthWriter
from p2278e_display.matrix import image_to_framebuffer, make_matrix
from p2278e_display.render import NowPlaying, render_now_playing
from p2278e_display.system_health import PowerThermalSampler
from p2278e_display.wiim import WiiMState, discover_wiim, get_now_playing

running = True


def stop(*_args) -> None:
    global running
    running = False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)

parser = argparse.ArgumentParser()
parser.add_argument("--duration", type=float, default=0)
parser.add_argument("--brightness", type=float, default=None)
parser.add_argument("--refresh", type=float, default=2.0)
parser.add_argument("--fps", type=float, choices=(20.0, 30.0), default=None)
parser.add_argument("--viz", choices=("levels", "spectrum"), default=None)
parser.add_argument("--base-url", default=None)
args = parser.parse_args()


def load_config() -> dict:
    try:
        data = json.loads((ROOT / "config.json").read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


config = load_config()
brightness = float(args.brightness if args.brightness is not None else config.get("brightness", 0.60))
fps = float(args.fps if args.fps is not None else config.get("fps", 30))
visualization = str(args.viz or config.get("visualization", "spectrum"))
no_info_timeout_s = float(config.get("no_info_timeout_seconds", 15 * 60))
base_url = str(args.base_url or config.get("wiim_base_url") or "")
friendly_name = str(config.get("friendly_name") or "")

lock_path = ROOT / "display.lock"
lock_file = lock_path.open("w")
try:
    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print(f"display already running; lock held at {lock_path}", flush=True)
    raise SystemExit(2)
lock_file.write(str(os.getpid()))
lock_file.flush()


def log(message: str) -> None:
    print(message, flush=True)


def notify_systemd(message: str) -> None:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode())
    except OSError:
        pass


def fetch_resilient(current_url: str) -> tuple[WiiMState, str]:
    if current_url:
        try:
            return get_now_playing(current_url), current_url
        except Exception as original_error:
            try:
                discovered = discover_wiim(friendly_name=friendly_name)
                return get_now_playing(discovered), discovered
            except Exception:
                raise original_error
    discovered = discover_wiim(friendly_name=friendly_name)
    return get_now_playing(discovered), discovered


def normalize_transport(value: str) -> Transport:
    key = (value or "").upper()
    return {
        "PLAYING": Transport.PLAYING,
        "PAUSED_PLAYBACK": Transport.PAUSED,
        "PAUSED": Transport.PAUSED,
        "STOPPED": Transport.STOPPED,
        "NO_MEDIA_PRESENT": Transport.NO_MEDIA,
        "TRANSITIONING": Transport.TRANSITIONING,
    }.get(key, Transport.UNKNOWN)


def observation_from_wiim(state: WiiMState, at: float) -> Observation:
    duration = parse_hms(state.duration)
    return Observation(
        at=at,
        ok=True,
        transport=normalize_transport(state.state),
        title=state.title or "",
        artist=state.artist or "",
        album=state.album or "",
        duration_s=int(duration) if duration is not None and duration > 0 else UNKNOWN_DURATION,
        art_uri=state.album_art_uri or "",
    )


def presentation_state(appliance: ApplianceState) -> str:
    if appliance.state is UserState.STARTING:
        return "starting"
    if appliance.state is UserState.SEARCHING:
        return "searching"
    if appliance.state is UserState.IDLE:
        return "idle"
    if appliance.paused:
        return "pause"
    if appliance.transitioning:
        return "transition"
    return "play"


art_cache_dir = ROOT / "cache" / "art"
levels_cache_dir = ROOT / "cache" / "levels"
spectrum_cache_dir = ROOT / "cache" / "spectrum"
# Keep one analyzer per supported mode for the lifetime of the process. Config
# reloads switch this pointer instead of abandoning a live worker thread.
analyzers = {
    "spectrum": AsyncSpectrumAnalyzer(spectrum_cache_dir, logger=log),
    "levels": AsyncLevelAnalyzer(levels_cache_dir, logger=log),
}
analyzer = analyzers[visualization]
log(f"visualization={visualization} fps={fps:g} brightness={brightness:.2f}")

started_mono = time.monotonic()
appliance = ApplianceState.initial(started_mono)
metadata_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wiim-fetch")
art_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="art-fetch")
metadata_future: Future | None = None
art_jobs: dict[Future, tuple[int, str, str]] = {}
current_art_path = ""
current_track_uri = ""
last_art_uri = ""
last_rel_seconds = 0.0
last_rel_mono = started_mono
last_transport = Transport.UNKNOWN
fetch_errors = 0
last_fetch = 0.0
last_health_write = 0.0
last_watchdog = 0.0
last_config_check = 0.0
config_mtime = 0.0
visible_levels = [0.0] * 9
peak_levels = [0.0] * 9
peak_until = [0.0] * 9

health_path = ROOT / "run" / "health.json"
health_writer = HealthWriter(health_path, log=log)
power_sampler = PowerThermalSampler(startup_grace_s=45, repeat_threshold=3)

initial = render_now_playing(
    NowPlaying(state="starting", visualization=visualization, analysis_available=False),
    brightness=brightness,
)
fb = image_to_framebuffer(initial)
matrix = make_matrix(fb)
matrix.show()
notify_systemd("READY=1\nSTATUS=Starting; discovering WiiM")

frame = 0
frame_interval = 1.0 / max(1.0, fps)
next_frame = time.monotonic()
fps_window_started = next_frame
fps_window_frames = 0
rolling_fps = fps

try:
    while running:
        t = time.monotonic()

        # Reload safe visual settings written atomically by the admin surface.
        if t - last_config_check >= 2.0:
            last_config_check = t
            try:
                mtime = (ROOT / "config.json").stat().st_mtime
            except OSError:
                mtime = config_mtime
            if mtime != config_mtime:
                config_mtime = mtime
                fresh = load_config()
                brightness = float(fresh.get("brightness", brightness))
                no_info_timeout_s = float(
                    fresh.get("no_info_timeout_seconds", no_info_timeout_s)
                )
                stale_seconds = float(fresh.get("stale_seconds", appliance_core.STALE_AFTER_S))
                clear_seconds = float(fresh.get("clear_seconds", appliance_core.FAULT_SEARCHING_AFTER_S))
                if 5 <= stale_seconds < clear_seconds <= 180:
                    appliance_core.STALE_AFTER_S = stale_seconds
                    appliance_core.FAULT_SEARCHING_AFTER_S = clear_seconds
                new_fps = float(fresh.get("fps", fps))
                if new_fps in (20.0, 30.0) and new_fps != fps:
                    fps = new_fps
                    frame_interval = 1.0 / fps
                    next_frame = t
                # Visualization changes are restart-bound. Switching while an
                # analyzer owns an ffmpeg worker can orphan that worker.
                friendly_name = str(fresh.get("friendly_name", friendly_name))

        # Metadata I/O is fully off the render thread.
        retry_interval = min(18.0, args.refresh * (2 ** min(fetch_errors, 3)))
        if metadata_future is None and t - last_fetch >= retry_interval:
            last_fetch = t
            metadata_future = metadata_pool.submit(fetch_resilient, base_url)

        metadata_result_processed = False
        if metadata_future is not None and metadata_future.done():
            metadata_result_processed = True
            try:
                wiim, resolved_url = metadata_future.result()
                if resolved_url != base_url:
                    base_url = resolved_url
                    log("WiiM renderer discovered")
                obs = observation_from_wiim(wiim, t)
                previous_generation = appliance.generation
                previous_track = appliance.content.track_id
                appliance = reduce(appliance, obs)
                fetch_errors = 0

                owner_changed = (
                    appliance.generation != previous_generation
                    or appliance.content.track_id != previous_track
                )
                if owner_changed:
                    current_art_path = ""
                    last_art_uri = ""
                    current_track_uri = wiim.track_uri or ""
                    visible_levels = [0.0] * 9
                    peak_levels = [0.0] * 9
                    peak_until = [0.0] * 9
                elif not current_track_uri and wiim.track_uri:
                    # A token rotation for the same stable track must not reset
                    # or re-key already-running analysis.
                    current_track_uri = wiim.track_uri

                last_rel_seconds = parse_hms(wiim.rel_time) or 0.0
                last_rel_mono = t
                last_transport = obs.transport

                # New-track artwork is fallback until this owned result lands.
                wanted_art = obs.art_uri
                if appliance.content.track_id and wanted_art and wanted_art != last_art_uri:
                    owner = (appliance.generation, appliance.content.track_id, wanted_art)
                    art_jobs[art_pool.submit(cache_album_art, wanted_art, art_cache_dir)] = owner
                    last_art_uri = wanted_art

                if current_track_uri:
                    analyzer.request(current_track_uri)
            except Exception as exc:
                fetch_errors += 1
                appliance = reduce(appliance, Observation.failure(t))
                log(f"display fetch error {type(exc).__name__}: {exc}")
            finally:
                metadata_future = None

        # Advance STARTING/stale/searching timers continuously between retries.
        # Do not apply the same failure twice on the iteration that consumed a
        # completed metadata result.
        if appliance.last_fail_at >= 0 and not metadata_result_processed:
            appliance = reduce(appliance, Observation.failure(t))

        for finished in [job for job in art_jobs if job.done()]:
            owner = art_jobs.pop(finished)
            try:
                path = str(finished.result() or "")
            except Exception:
                path = ""
            result = AsyncResult(owner[0], owner[1], "art", bool(path))
            if result_is_current(appliance, result):
                appliance = apply_async_result(appliance, result)
                current_art_path = path
            else:
                log(f"discarded stale art result generation={owner[0]}")

        # Sample only the current track's analyzer cache.
        real_levels: tuple[float, ...] = ()
        if current_track_uri and last_transport is Transport.PLAYING:
            rel = last_rel_seconds + max(0.0, t - last_rel_mono)
            real_levels = analyzer.levels_at_seconds(current_track_uri, rel)

        targets = list(real_levels[:9]) if real_levels else []
        if targets:
            while len(targets) < 9:
                targets.append(0.0)
            for i, target in enumerate(targets[:9]):
                coef = 0.72 if target > visible_levels[i] else 0.18
                visible_levels[i] += (target - visible_levels[i]) * coef
        else:
            for i in range(9):
                visible_levels[i] *= 0.86

        meter_levels = tuple(visible_levels) if targets else ()
        peaks: tuple[float, ...] = ()
        if visualization == "spectrum" and targets:
            for i, level in enumerate(visible_levels):
                if level >= peak_levels[i]:
                    peak_levels[i] = level
                    peak_until[i] = t + 0.22
                elif t > peak_until[i]:
                    peak_levels[i] *= 0.92
            peaks = tuple(peak_levels)

        show_art = "" if appliance.use_fallback_art else current_art_path
        progress = 0.0
        if appliance.duration_known and appliance.content.duration_s > 0:
            rel = last_rel_seconds
            if last_transport is Transport.PLAYING:
                rel += max(0.0, t - last_rel_mono)
            progress = max(0.0, min(1.0, rel / appliance.content.duration_s))

        presentation = NowPlaying(
            title=appliance.content.title,
            artist=appliance.content.artist,
            state=presentation_state(appliance),
            progress=progress,
            album_art_path=show_art,
            levels=meter_levels,
            peaks=peaks,
            visualization=visualization,
            stale=appliance.stale,
            analysis_available=bool(real_levels),
            duration_known=appliance.duration_known,
        )
        last_info_at = (
            appliance.last_ok_at if appliance.last_ok_at >= 0 else appliance.started_at
        )
        no_info_seconds = max(0.0, t - last_info_at)
        sleeping_no_info = should_sleep_no_info(
            appliance, t, no_info_timeout_s
        )
        img = render_now_playing(
            presentation,
            brightness=0.0 if sleeping_no_info else brightness,
            frame=frame,
        )
        fb[:] = image_to_framebuffer(img)
        matrix.show()
        frame += 1
        fps_window_frames += 1

        # Wall-time paced, but emitted only after successful frame progress.
        if frame == 1 or t - last_watchdog >= 5.0:
            notify_systemd("WATCHDOG=1")
            last_watchdog = t

        if t - fps_window_started >= 5.0:
            rolling_fps = fps_window_frames / max(0.001, t - fps_window_started)
            fps_window_started = t
            fps_window_frames = 0

        if t - last_health_write >= 5.0:
            health_writer.write({
                "updated_at": time.time(),
                "uptime_seconds": round(t - started_mono, 1),
                "frames": frame,
                "estimated_fps": round(rolling_fps, 1),
                "canonical_state": appliance.state.value,
                "paused": appliance.paused,
                "stale": appliance.stale,
                "transitioning": appliance.transitioning,
                "generation": appliance.generation,
                "track_id": appliance.content.track_id,
                "wiim_state": last_transport.value,
                "title": appliance.content.title,
                "artist": appliance.content.artist,
                "consecutive_fetch_errors": fetch_errors,
                "visualization": visualization,
                "brightness": brightness,
                "sleeping_no_info": sleeping_no_info,
                "no_info_seconds": round(no_info_seconds, 1),
                "no_info_timeout_seconds": no_info_timeout_s,
                "fps_target": fps,
                "flags": {
                    "art_degraded": appliance.art_degraded,
                    "analysis_degraded": (
                        appliance.state is UserState.NOW_PLAYING
                        and not appliance.paused
                        and bool(current_track_uri)
                        and not bool(real_levels)
                    ),
                    "analysis_live": bool(real_levels),
                },
                "power": power_sampler.sample(),
            })
            last_health_write = t

        if args.duration and t - started_mono >= args.duration:
            break
        next_frame += frame_interval
        sleep_for = next_frame - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)
        else:
            next_frame = time.monotonic()
finally:
    notify_systemd("STOPPING=1\nSTATUS=Stopping matrix renderer")
    fb[:] = 0
    matrix.show()
    metadata_pool.shutdown(wait=False, cancel_futures=True)
    art_pool.shutdown(wait=False, cancel_futures=True)
    fcntl.flock(lock_file, fcntl.LOCK_UN)
