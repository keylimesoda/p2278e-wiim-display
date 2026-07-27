#!/usr/bin/env bash
set -euo pipefail
found=0
while read -r pid args; do
  [ -z "${pid:-}" ] && continue
  case "$args" in
    "python scripts/display_live.py"*) echo "stopping $pid $args"; kill "$pid" 2>/dev/null || true; found=1 ;;
  esac
done < <(ps -eo pid=,args=)
if [ "$found" = 0 ]; then
  echo "no display_live process found"
fi
