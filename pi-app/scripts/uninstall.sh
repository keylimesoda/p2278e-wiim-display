#!/usr/bin/env bash
set -euo pipefail

# Removes only the systemd units installed by scripts/install.sh.
# Source, configuration, tokens and caches are preserved unless --purge-state is used.

APP_DIR="${P2278E_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
UNIT_DIR="${P2278E_UNIT_DIR:-/etc/systemd/system}"
PURGE=false
[[ "${1:-}" == "--purge-state" ]] && PURGE=true

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo ./scripts/uninstall.sh [--purge-state]" >&2
  exit 1
fi

systemctl disable --now p2278e-display.service p2278e-admin.service 2>/dev/null || true
rm -f "$UNIT_DIR/p2278e-display.service" "$UNIT_DIR/p2278e-admin.service"
systemctl daemon-reload
systemctl reset-failed p2278e-display.service p2278e-admin.service 2>/dev/null || true

if $PURGE; then
  rm -rf "$APP_DIR/run" "$APP_DIR/cache" "$APP_DIR/logs"
  echo "Removed services and runtime state. Source and config.json were preserved."
else
  echo "Removed services. Source, config, token, cache and logs were preserved."
fi
