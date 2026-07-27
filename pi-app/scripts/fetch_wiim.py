#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from p2278e_display.wiim import get_now_playing

parser = argparse.ArgumentParser()
parser.add_argument("--base-url", default=None)
args = parser.parse_args()
config = json.loads((ROOT / "config.json").read_text())
base_url = args.base_url or config["wiim_base_url"]
state = get_now_playing(base_url)
print(json.dumps(state.to_dict(), indent=2, sort_keys=True))
