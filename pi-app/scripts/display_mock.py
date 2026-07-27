#!/usr/bin/env python3
import argparse
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from p2278e_display.matrix import image_to_framebuffer, make_matrix
from p2278e_display.render import NowPlaying, render_now_playing

running = True

def stop(*_):
    global running
    running = False

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)

parser = argparse.ArgumentParser()
parser.add_argument("--duration", type=float, default=0, help="seconds to display; 0 = until Ctrl-C")
parser.add_argument("--brightness", type=float, default=0.35)
args = parser.parse_args()

started = time.monotonic()
frame = 0
img = render_now_playing(NowPlaying(), brightness=args.brightness)
framebuffer = image_to_framebuffer(img)
matrix = make_matrix(framebuffer)

while running:
    progress = ((frame % 120) / 119.0)
    img = render_now_playing(NowPlaying(progress=progress), brightness=args.brightness)
    framebuffer[:] = image_to_framebuffer(img)
    matrix.show()
    frame += 1
    if args.duration and time.monotonic() - started >= args.duration:
        break
    time.sleep(0.1)

framebuffer[:] = 0
matrix.show()
