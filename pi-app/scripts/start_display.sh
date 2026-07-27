#!/usr/bin/env bash
set -euo pipefail
BRIGHTNESS="${1:-0.60}"
DURATION="${2:-0}"
FPS="${3:-30}"
VIZ="${4:-spectrum}"
APP_DIR="${P2278E_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
VENV_DIR="${P2278E_VENV_DIR:-$APP_DIR/.venv}"
cd "$APP_DIR"
source "$VENV_DIR/bin/activate"
mkdir -p logs
# Stop any existing display_live instance so only one process owns the matrix.
while read -r pid args; do
  [ -z "${pid:-}" ] && continue
  case "$args" in
    "python scripts/display_live.py"*) kill "$pid" 2>/dev/null || true ;;
  esac
done < <(ps -eo pid=,args=)
sleep 1
nohup bash -lc "source '$VENV_DIR/bin/activate' && cd '$APP_DIR' && python scripts/display_live.py --duration '$DURATION' --brightness '$BRIGHTNESS' --fps '$FPS' --viz '$VIZ'" > "logs/display-live-$(date +%Y%m%d-%H%M%S)-b${BRIGHTNESS}-${VIZ}.log" 2>&1 &
echo "display_live_pid=$! brightness=$BRIGHTNESS duration=$DURATION fps=$FPS viz=$VIZ"
