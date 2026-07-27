#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from p2278e_display.render import NowPlaying, render_now_playing

parser = argparse.ArgumentParser()
parser.add_argument("--out", default="output/mock-now-playing.png")
parser.add_argument("--brightness", type=float, default=0.55)
parser.add_argument("--viz", choices=("levels", "spectrum"), default="levels")
args = parser.parse_args()

img = render_now_playing(NowPlaying(visualization=args.viz), brightness=args.brightness)
out = ROOT / args.out
out.parent.mkdir(parents=True, exist_ok=True)
img.save(out)
print(out)
