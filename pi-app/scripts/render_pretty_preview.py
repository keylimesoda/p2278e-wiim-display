#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from p2278e_display.art_cache import cache_album_art
from p2278e_display.render import NowPlaying, render_now_playing
from p2278e_display.wiim import get_now_playing

parser = argparse.ArgumentParser()
parser.add_argument("--out", default="output/pretty-now-playing.png")
parser.add_argument("--brightness", type=float, default=0.75)
args = parser.parse_args()
config = json.loads((ROOT / "config.json").read_text())
s = get_now_playing(config["wiim_base_url"])
art_path = cache_album_art(s.album_art_uri, ROOT / "cache" / "art") if s.album_art_uri else ""
now = NowPlaying(title=s.title, artist=s.artist, source="WiiM", state=("play" if s.state == "PLAYING" else "pause"), progress=s.progress, album_art_path=art_path)
img = render_now_playing(now, brightness=args.brightness, frame=3)
out = ROOT / args.out
out.parent.mkdir(parents=True, exist_ok=True)
img.save(out)
print(out)
