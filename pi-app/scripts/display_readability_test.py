#!/usr/bin/env python3
import argparse
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image, ImageDraw, ImageFont
from p2278e_display.matrix import image_to_framebuffer, make_matrix

running = True

def stop(*_):
    global running
    running = False

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)

parser = argparse.ArgumentParser(description="P2278E panel readability / brightness / scrolling test")
parser.add_argument("--duration", type=float, default=0, help="seconds to display; 0 = until Ctrl-C")
parser.add_argument("--brightness", type=float, default=0.35)
args = parser.parse_args()

font = ImageFont.load_default()
started = time.monotonic()
frame = 0
img = Image.new("RGB", (64, 32), (0, 0, 0))
fb = image_to_framebuffer(img)
matrix = make_matrix(fb)

palette = [
    (255, 0, 0), (0, 255, 0), (0, 0, 255),
    (255, 255, 0), (0, 255, 255), (255, 0, 255), (255, 255, 255),
]

def dim(c):
    b = max(0.0, min(1.0, args.brightness))
    return tuple(int(x * b) for x in c)

long_title = "This Is A Long Track Title"
while running:
    img.paste((0, 0, 0), (0, 0, 64, 32))
    d = ImageDraw.Draw(img)

    # Color/gamma bars across top: useful for channel order and perceived brightness.
    for i, color in enumerate(palette):
        x0 = i * 9
        d.rectangle((x0, 0, x0 + 7, 4), fill=dim(color))

    # Static tiny text samples.
    d.text((1, 7), "ABC123", font=font, fill=dim((255, 255, 255)))
    d.text((1, 15), "WiiM", font=font, fill=dim((0, 180, 210)))
    d.rectangle((1, 26, 62, 28), fill=dim((25, 25, 25)))
    d.rectangle((1, 26, 1 + int((frame % 100) / 99 * 61), 28), fill=dim((60, 210, 120)))

    # Scroll sample in right half.
    text_w = int(d.textlength(long_title, font=font))
    window_x0, window_x1 = 33, 63
    scroll_span = text_w + (window_x1 - window_x0 + 1)
    offset = frame % max(1, scroll_span)
    x = window_x1 - offset
    # clip by drawing to small temporary strip
    strip = Image.new("RGB", (31, 8), (0, 0, 0))
    sd = ImageDraw.Draw(strip)
    sd.text((x - window_x0, 0), long_title, font=font, fill=dim((220, 220, 220)))
    img.paste(strip, (33, 15))

    fb[:] = image_to_framebuffer(img)
    matrix.show()
    frame += 1
    if args.duration and time.monotonic() - started >= args.duration:
        break
    time.sleep(0.08)

fb[:] = 0
matrix.show()
