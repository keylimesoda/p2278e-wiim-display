"""Pixel/identity tests for calm P2278E presentation states."""
from pathlib import Path

from PIL import Image

from p2278e_display.render import (
    FLOOR_LEVEL,
    STALE_PULSE_ON,
    STALE_PULSE_PERIOD,
    WIDTH,
    NowPlaying,
    render_now_playing,
    stale_pulse_on,
)


def _cover(tmp_path: Path, color=(80, 120, 200)) -> str:
    path = tmp_path / "cover.png"
    Image.new("RGB", (128, 128), color).save(path)
    return str(path)


def _progress_row_lit(img: Image.Image) -> bool:
    """Return True if the progress lane (y=12, x in 35..61) has any lit pixel."""
    for x in range(35, 62):
        if img.getpixel((x, 12)) != (0, 0, 0):
            return True
    return False


# --- Unknown duration hides the progress bar -------------------------------

def test_unknown_duration_hides_progress(tmp_path: Path):
    art = _cover(tmp_path)
    known = render_now_playing(
        NowPlaying(state="play", progress=0.5, album_art_path=art, duration_known=True),
        brightness=1.0,
    )
    unknown = render_now_playing(
        NowPlaying(state="play", progress=0.5, album_art_path=art, duration_known=False),
        brightness=1.0,
    )
    assert _progress_row_lit(known)
    assert not _progress_row_lit(unknown)


# --- Static floor invariant across frames when analysis unavailable --------

def test_static_floor_invariant_across_frames(tmp_path: Path):
    art = _cover(tmp_path)
    now = NowPlaying(title="Air", artist="", state="play", album_art_path=art, analysis_available=False, levels=())
    a = render_now_playing(now, brightness=1.0, frame=0)
    b = render_now_playing(now, brightness=1.0, frame=17)
    c = render_now_playing(now, brightness=1.0, frame=250)
    assert a.tobytes() == b.tobytes() == c.tobytes()
    # And the floor pixel is present at the dim floor color.
    assert a.getpixel((35, 31)) == FLOOR_LEVEL


def test_non_playing_uses_static_floor_not_animation(tmp_path: Path):
    art = _cover(tmp_path)
    # Paused with no analysis: floor is static across frames.
    now = NowPlaying(title="Air", artist="", state="pause", album_art_path=art, analysis_available=True, levels=())
    a = render_now_playing(now, brightness=1.0, frame=1)
    b = render_now_playing(now, brightness=1.0, frame=99)
    assert a.tobytes() == b.tobytes()


# --- Short title is static (frame invariant) -------------------------------

def test_short_title_is_static(tmp_path: Path):
    art = _cover(tmp_path)
    now = NowPlaying(title="Air", artist="", state="play", album_art_path=art, levels=())
    a = render_now_playing(now, brightness=1.0, frame=0)
    b = render_now_playing(now, brightness=1.0, frame=40)
    # Title strip region rows 0..10, x 33..63 must be identical across frames.
    for y in range(0, 11):
        for x in range(33, 64):
            assert a.getpixel((x, y)) == b.getpixel((x, y))


def test_long_title_scrolls(tmp_path: Path):
    art = _cover(tmp_path)
    now = NowPlaying(
        title="A Very Long Track Title That Cannot Possibly Fit",
        artist="With A Long Artist Name Too",
        state="play",
        album_art_path=art,
        levels=(),
    )
    a = render_now_playing(now, brightness=1.0, frame=0)
    b = render_now_playing(now, brightness=1.0, frame=30)
    diff = any(
        a.getpixel((x, y)) != b.getpixel((x, y))
        for y in range(0, 11)
        for x in range(33, 64)
    )
    assert diff


# --- Stale cue toggles ------------------------------------------------------

def test_stale_pulse_gate_toggles():
    assert stale_pulse_on(0) is True
    assert stale_pulse_on(STALE_PULSE_ON - 1) is True
    assert stale_pulse_on(STALE_PULSE_ON) is False
    assert stale_pulse_on(STALE_PULSE_PERIOD - 1) is False
    assert stale_pulse_on(STALE_PULSE_PERIOD) is True


def test_stale_cue_renders_amber_pixel_toggle(tmp_path: Path):
    art = _cover(tmp_path)
    stale = NowPlaying(state="play", album_art_path=art, stale=True, levels=())
    not_stale = NowPlaying(state="play", album_art_path=art, stale=False, levels=())
    corner = (WIDTH - 1, 0)
    on_frame = render_now_playing(stale, brightness=1.0, frame=0)
    off_frame = render_now_playing(stale, brightness=1.0, frame=STALE_PULSE_ON)
    clean = render_now_playing(not_stale, brightness=1.0, frame=0)
    assert on_frame.getpixel(corner) != (0, 0, 0)
    assert off_frame.getpixel(corner) == clean.getpixel(corner)


# --- State visual distinctions ---------------------------------------------

def test_status_screens_are_distinct(tmp_path: Path):
    art = _cover(tmp_path)
    starting = render_now_playing(NowPlaying(state="starting", album_art_path=art), brightness=1.0)
    searching = render_now_playing(NowPlaying(state="searching", album_art_path=art), brightness=1.0)
    idle = render_now_playing(NowPlaying(state="idle", album_art_path=art), brightness=1.0)
    playing = render_now_playing(
        NowPlaying(state="play", album_art_path=art, levels=(0.5,) * 9), brightness=1.0
    )
    frames = [x.tobytes() for x in (starting, searching, idle, playing)]
    # All four must be visually distinct from each other.
    for i in range(len(frames)):
        for j in range(i + 1, len(frames)):
            assert frames[i] != frames[j]


def test_status_screens_ignore_real_art(tmp_path: Path):
    # Status screens use fallback art, not the (possibly stale) real cover.
    red = _cover(tmp_path / "" if False else tmp_path, color=(240, 10, 10))
    idle = render_now_playing(NowPlaying(state="idle", album_art_path=red), brightness=1.0)
    # Fallback art is not solid red anywhere in the art pane.
    assert not all(
        idle.getpixel((x, y)) == (240, 10, 10) for y in range(0, 32) for x in range(0, 32)
    )


def test_paused_keeps_real_art_and_frozen_progress(tmp_path: Path):
    art = _cover(tmp_path, color=(80, 120, 200))
    paused = render_now_playing(
        NowPlaying(state="pause", progress=0.6, album_art_path=art, duration_known=True, levels=()),
        brightness=1.0,
    )
    playing = render_now_playing(
        NowPlaying(state="play", progress=0.6, album_art_path=art, duration_known=True, levels=()),
        brightness=1.0,
    )
    # Progress present in both.
    assert _progress_row_lit(paused)
    # Frozen (amber-ish) fill differs from the live cyan fill.
    pause_fill = paused.getpixel((37, 12))
    play_fill = playing.getpixel((37, 12))
    assert pause_fill != play_fill
    assert pause_fill[0] >= pause_fill[2]  # warmer (frozen) than cyan


def test_playing_shows_meter_when_levels_present(tmp_path: Path):
    art = _cover(tmp_path)
    now = NowPlaying(state="play", album_art_path=art, levels=(0.9,) * 9)
    img = render_now_playing(now, brightness=1.0, frame=0)
    # Meter lane has lit pixels above the floor row.
    assert any(img.getpixel((35, y)) != (0, 0, 0) for y in range(16, 31))
