from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Tuple
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
import numpy as np

WIDTH = 64
HEIGHT = 32
BLACK = (0, 0, 0)
WHITE = (235, 235, 230)
GREY = (34, 36, 38)
AMBER = (235, 150, 20)

# Presentation states. These describe *how the screen should read*, decoupled
# from appliance-core state so we never import the core (avoids cycles).
STATE_PLAY = "play"
STATE_PAUSE = "pause"
STATE_STARTING = "starting"
STATE_IDLE = "idle"
STATE_SEARCHING = "searching"
STATE_TRANSITION = "transition"
_PLAYING_STATES = frozenset({STATE_PLAY})

# Honest fallback words per non-playing state. Never technical error prose.
_STATE_WORD = {
    STATE_STARTING: "Connecting",
    STATE_SEARCHING: "Searching",
    STATE_IDLE: "Idle",
    STATE_TRANSITION: "",
}

# Effective brightness floor: a lit panel should never dip below this when a
# nonzero brightness was requested. Zero stays truly black for APIs that expect
# an off panel.
MIN_EFFECTIVE_BRIGHTNESS = 0.25

# Static dim floor row when the meter has no live analysis (calm, not animated).
FLOOR_LEVEL = (0, 60, 26)

# Stale amber pulse cadence, in frames. A short on-window keeps it a quiet cue.
STALE_PULSE_PERIOD = 30
STALE_PULSE_ON = 8

# Display-side meter curve. The analyzer normalizes per track, but many modern
# tracks still spend too much time near 1.0. Keep the body active in
# green/yellow and reserve red for real peaks.
METER_LEVEL_GAMMA = 1.28
METER_LEVEL_GAIN = 0.86

Color = Tuple[int, int, int]
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOTO_FONT = PROJECT_ROOT / "assets" / "fonts" / "Doto-Bold.ttf"


@dataclass(frozen=True)
class NowPlaying:
    title: str = "Now Playing"
    artist: str = "Example Artist"
    source: str = "WiiM"
    state: str = "play"
    progress: float = 0.42
    album_art_path: str = ""
    levels: tuple[float, ...] = ()
    peaks: tuple[float, ...] = ()
    visualization: str = "levels"
    # Presentation modifiers (all optional; defaults keep legacy callers valid).
    stale: bool = False
    analysis_available: bool = True
    duration_known: bool = True

    def is_playing(self) -> bool:
        return self.state in _PLAYING_STATES

    def is_status_screen(self) -> bool:
        """starting / searching / idle render a fallback status screen."""
        return self.state in (STATE_STARTING, STATE_SEARCHING, STATE_IDLE)


def clamp(v: float) -> int:
    return int(max(0, min(255, v)))


def mix(a: Color, b: Color, t: float) -> Color:
    return (clamp(a[0] * (1 - t) + b[0] * t), clamp(a[1] * (1 - t) + b[1] * t), clamp(a[2] * (1 - t) + b[2] * t))


def scale_color(color: Color, brightness: float) -> Color:
    b = max(0.0, min(1.0, brightness))
    return (clamp(color[0] * b), clamp(color[1] * b), clamp(color[2] * b))


def effective_brightness(brightness: float) -> float:
    """Clamp to the floor when lit; preserve a true-black off state at zero."""
    if brightness <= 0.0:
        return 0.0
    b = min(1.0, brightness)
    return max(MIN_EFFECTIVE_BRIGHTNESS, b)


def apply_brightness(img: Image.Image, brightness: float) -> Image.Image:
    """Vectorized brightness scaling. Floors nonzero brightness at 0.25."""
    b = effective_brightness(brightness)
    if b >= 0.999:
        return img
    if b <= 0.0:
        return Image.new("RGB", img.size, BLACK)
    arr = np.asarray(img.convert("RGB"), dtype=np.float32)
    scaled = np.clip(arr * b + 0.5, 0, 255).astype(np.uint8)
    return Image.fromarray(scaled, "RGB")


def fallback_art() -> Image.Image:
    art = Image.new("RGB", (32, 32), (8, 8, 12))
    d = ImageDraw.Draw(art)
    for y in range(0, 32, 4):
        for x in range(0, 32, 4):
            d.rectangle((x, y, x + 3, y + 3), fill=(30 + x * 3, 30 + y * 3, 70 + (x + y) * 2))
    return art


def load_album_art(path: str) -> Image.Image:
    if not path:
        return fallback_art()
    try:
        return Image.open(Path(path)).convert("RGB")
    except Exception:
        return fallback_art()


def fit_square_without_cropping(img: Image.Image) -> Image.Image:
    """Preserve the complete cover; letterbox only malformed non-square art."""
    if img.width == img.height:
        return img
    side = max(img.size)
    contained = ImageOps.contain(img, (side, side), Image.Resampling.LANCZOS)
    square = Image.new("RGB", (side, side), BLACK)
    square.paste(contained, ((side - contained.width) // 2, (side - contained.height) // 2))
    return square


def linear_light_area_resize(img: Image.Image, size: int) -> Image.Image:
    """Area-resample RGB in linear light, then encode back to sRGB."""
    srgb = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0
    linear = np.where(srgb <= 0.04045, srgb / 12.92, ((srgb + 0.055) / 1.055) ** 2.4)
    channels = []
    for index in range(3):
        channel = Image.fromarray(linear[:, :, index], mode="F")
        channels.append(np.asarray(channel.resize((size, size), Image.Resampling.BOX), dtype=np.float32))
    linear_resized = np.clip(np.stack(channels, axis=2), 0.0, 1.0)
    encoded = np.where(
        linear_resized <= 0.0031308,
        linear_resized * 12.92,
        1.055 * np.power(linear_resized, 1.0 / 2.4) - 0.055,
    )
    return Image.fromarray(np.uint8(np.clip(encoded * 255.0 + 0.5, 0, 255)), "RGB")


@lru_cache(maxsize=32)
def album_tile(path: str) -> Image.Image:
    """Build and cache one display-ready tile per cover, never per frame."""
    source = fit_square_without_cropping(load_album_art(path))
    intermediate = linear_light_area_resize(source, 64)
    sharpened = intermediate.filter(ImageFilter.UnsharpMask(radius=0.75, percent=65, threshold=2))
    return linear_light_area_resize(sharpened, 32)


def doto_font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if DOTO_FONT.exists():
        return ImageFont.truetype(str(DOTO_FONT), 10)
    return ImageFont.load_default()


def meter_color(segment_index: int, segment_count: int) -> Color:
    """Classic LED level-meter colors: green body, yellow shoulder, red peak."""
    if segment_count <= 1:
        return (0, 220, 90)
    t = segment_index / (segment_count - 1)
    if t < 0.62:
        return (0, 220, 90)
    if t < 0.84:
        return (235, 210, 40)
    return (255, 70, 45)


def shape_meter_level(level: float) -> float:
    """Lift normalized band levels without throwing away quiet bands."""
    level = max(0.0, min(1.0, level))
    return max(0.0, min(1.0, pow(level, METER_LEVEL_GAMMA) * METER_LEVEL_GAIN))


def stale_pulse_on(frame: int) -> bool:
    """Deterministic amber-pulse gate for the stale cue. Testable by frame."""
    return (frame % STALE_PULSE_PERIOD) < STALE_PULSE_ON


def draw_stale_cue(base: Image.Image, frame: int) -> None:
    """1px amber pulse in the top-right corner. Quiet, no shimmer complexity."""
    if stale_pulse_on(frame):
        base.putpixel((WIDTH - 1, 0), AMBER)


def measure_title(now: NowPlaying) -> tuple[str, int, int]:
    """Return (title_text, text_width, strip_width)."""
    font = doto_font()
    title = now.title or "Idle"
    if now.artist and now.artist not in title:
        title = f"{title}  {now.artist}"
    strip_w = 31
    probe = Image.new("RGB", (strip_w, 11), BLACK)
    text_w = int(ImageDraw.Draw(probe).textlength(title, font=font))
    return title, text_w, strip_w


def draw_title_flow(base: Image.Image, now: NowPlaying, frame: int) -> None:
    font = doto_font()
    title, text_w, strip_w = measure_title(now)
    strip_h = 11
    strip = Image.new("RGB", (strip_w, strip_h), BLACK)
    sd = ImageDraw.Draw(strip)
    if text_w <= strip_w:
        # Short title: static, no scroll. Left-aligned, frame-invariant.
        sd.text((0, -2), title, font=font, fill=WHITE)
    else:
        span = max(1, text_w + strip_w + 8)
        x = strip_w - (((frame // 3) * 1) % span)
        sd.text((x, -2), title, font=font, fill=WHITE)
    base.paste(strip, (33, 0))


def draw_status_word(base: Image.Image, word: str) -> None:
    """Static centered status word in the right pane (Connecting/Searching/Idle)."""
    font = doto_font()
    strip = Image.new("RGB", (31, 11), BLACK)
    sd = ImageDraw.Draw(strip)
    text_w = int(sd.textlength(word, font=font))
    x = max(0, (31 - text_w) // 2)
    sd.text((x, -2), word, font=font, fill=WHITE)
    base.paste(strip, (33, 10))


def draw_progress(d: ImageDraw.ImageDraw, now: NowPlaying, frozen: bool = False) -> None:
    """Progress bar. Hidden entirely when duration is unknown."""
    if not now.duration_known:
        return
    x0, x1, y = 35, 61, 12
    d.line((x0, y, x1, y), fill=GREY)
    p = max(0.0, min(1.0, now.progress))
    xe = int(x0 + (x1 - x0) * p)
    # Paused keeps a colored (frozen) progress fill; playing uses live cyan.
    fill = (210, 150, 0) if frozen else (0, 210, 210)
    if xe >= x0:
        d.line((x0, y, xe, y), fill=fill)
        d.point((xe, y), fill=WHITE)


def draw_static_floor(d: ImageDraw.ImageDraw) -> None:
    """Calm 1px dim floor across the meter lane. No animation."""
    bottom = 31
    for idx in range(9):
        x0 = 35 + idx * 3
        d.line((x0, bottom, x0 + 1, bottom), fill=FLOOR_LEVEL)


def draw_levels_meter(d: ImageDraw.ImageDraw, now: NowPlaying) -> None:
    # Solid bottom-anchored columns. No inactive ghost pixels, no chart bg.
    bottom = 31
    max_height = 16
    levels = list(now.levels[:9]) if now.levels else []
    for idx in range(9):
        raw_level = levels[idx] if idx < len(levels) else 0.0
        level = shape_meter_level(raw_level)
        height = max(0, min(max_height, int(round(level * max_height))))
        if height <= 0:
            continue
        x0 = 35 + idx * 3
        x1 = x0 + 1
        for dy in range(height):
            seg = int((dy / max(1, max_height - 1)) * 7)
            d.line((x0, bottom - dy, x1, bottom - dy), fill=meter_color(seg, 8))


def draw_spectrum_analyzer(d: ImageDraw.ImageDraw, now: NowPlaying) -> None:
    """9 log-spaced FFT bands, bass left -> treble right, with peak-hold dots."""
    bottom = 31
    max_height = 17
    levels = list(now.levels[:9]) if now.levels else []
    peaks = list(now.peaks[:9]) if now.peaks else []
    for idx in range(9):
        raw_level = levels[idx] if idx < len(levels) else 0.0
        level = max(0.0, min(1.0, pow(max(0.0, raw_level), 1.04) * 0.98))
        height = max(0, min(max_height, int(round(level * max_height))))
        x0 = 35 + idx * 3
        x1 = x0 + 1
        for dy in range(height):
            seg = int((dy / max(1, max_height - 1)) * 7)
            d.line((x0, bottom - dy, x1, bottom - dy), fill=meter_color(seg, 8))
        if peaks:
            peak_level = max(0.0, min(1.0, peaks[idx] if idx < len(peaks) else 0.0))
            peak_y = bottom - max(0, min(max_height, int(round(peak_level * max_height))))
            if 14 <= peak_y <= bottom:
                d.line((x0, peak_y, x1, peak_y), fill=(235, 235, 230))


def _draw_meter_lane(d: ImageDraw.ImageDraw, now: NowPlaying) -> None:
    """Draw the live meter, or a static dim floor when it should stay calm."""
    if not now.analysis_available or not now.is_playing() or not now.levels:
        draw_static_floor(d)
        return
    if now.visualization == "spectrum":
        draw_spectrum_analyzer(d, now)
    else:
        draw_levels_meter(d, now)


def render_status_screen(now: NowPlaying, frame: int) -> Image.Image:
    """Fallback art + honest status word for starting/searching/idle."""
    img = Image.new("RGB", (WIDTH, HEIGHT), BLACK)
    img.paste(fallback_art(), (0, 0))
    d = ImageDraw.Draw(img)
    d.line((32, 0, 32, 31), fill=(18, 18, 18))
    word = _STATE_WORD.get(now.state, "")
    if word:
        draw_status_word(img, word)
    draw_static_floor(d)
    if now.stale:
        draw_stale_cue(img, frame)
    return img


def render_now_playing(
    now: NowPlaying | None = None,
    brightness: float = 0.60,
    frame: int = 0,
    dev_demo_animation: bool = False,
) -> Image.Image:
    now = now or NowPlaying()

    if now.is_status_screen():
        img = render_status_screen(now, frame)
        return apply_brightness(img, brightness)

    cover = album_tile(now.album_art_path)
    img = Image.new("RGB", (WIDTH, HEIGHT), BLACK)
    img.paste(cover, (0, 0))
    d = ImageDraw.Draw(img)
    d.line((32, 0, 32, 31), fill=(18, 18, 18))
    draw_title_flow(img, now, frame)

    paused = now.state == STATE_PAUSE
    draw_progress(d, now, frozen=paused)

    if dev_demo_animation and not now.levels:
        # Optional dev-only demo animation, never on the shipping default path.
        _draw_demo_animation(d, now, frame)
    else:
        _draw_meter_lane(d, now)

    if now.stale:
        draw_stale_cue(img, frame)
    return apply_brightness(img, brightness)


def _draw_demo_animation(d: ImageDraw.ImageDraw, now: NowPlaying, frame: int) -> None:
    """Dev-only synthetic meter motion. Not used on shipping path by default."""
    import math

    bottom = 31
    max_height = 16
    for idx in range(9):
        wave = 0.5 + 0.5 * math.sin(frame * 0.22 + idx * 0.74)
        slow = 0.5 + 0.5 * math.sin(frame * 0.07 + idx * 1.31)
        level = shape_meter_level(0.18 + 0.58 * wave + 0.22 * slow)
        height = max(0, min(max_height, int(round(level * max_height))))
        if height <= 0:
            continue
        x0 = 35 + idx * 3
        for dy in range(height):
            seg = int((dy / max(1, max_height - 1)) * 7)
            d.line((x0, bottom - dy, x0 + 1, bottom - dy), fill=meter_color(seg, 8))
