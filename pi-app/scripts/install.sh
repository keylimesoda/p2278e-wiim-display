#!/usr/bin/env bash
set -euo pipefail

# Installs the P2278E appliance from a cloned repository on Raspberry Pi OS.
# This script creates only p2278e-display.service and p2278e-admin.service.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${P2278E_APP_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
APP_USER="${P2278E_USER:-${SUDO_USER:-$(id -un)}}"
APP_GROUP="${P2278E_GROUP:-$(id -gn "$APP_USER")}"
VENV_DIR="${P2278E_VENV_DIR:-$APP_DIR/.venv}"
ADMIN_BIND="${P2278E_ADMIN_BIND:-127.0.0.1}"
UNIT_DIR="${P2278E_UNIT_DIR:-/etc/systemd/system}"

reject_unsafe_value() {
  local label="$1" value="$2"
  if [[ "$value" == *$'\n'* || "$value" == *$'\r'* || "$value" == *'|'* || "$value" == *'&'* ]]; then
    echo "$label contains an unsupported character (newline, |, or &)." >&2
    exit 1
  fi
}

[[ "$APP_USER" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]] || {
  echo "Invalid P2278E_USER: $APP_USER" >&2; exit 1;
}
[[ "$APP_GROUP" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]] || {
  echo "Invalid P2278E_GROUP: $APP_GROUP" >&2; exit 1;
}
[[ "$ADMIN_BIND" =~ ^[A-Za-z0-9.:_-]+$ ]] || {
  echo "Invalid P2278E_ADMIN_BIND: $ADMIN_BIND" >&2; exit 1;
}
for pair in "APP_DIR:$APP_DIR" "VENV_DIR:$VENV_DIR" "UNIT_DIR:$UNIT_DIR"; do
  reject_unsafe_value "${pair%%:*}" "${pair#*:}"
done
if [[ "$APP_DIR" == *' '* || "$VENV_DIR" == *' '* || "$UNIT_DIR" == *' '* ]]; then
  echo "Installation paths containing spaces are not supported." >&2
  exit 1
fi

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo ./scripts/install.sh" >&2
  exit 1
fi

for path in "$APP_DIR/scripts/display_live.py" "$APP_DIR/scripts/admin_server.py" \
  "$APP_DIR/systemd/p2278e-display.service" "$APP_DIR/systemd/p2278e-admin.service"; do
  [[ -e "$path" ]] || { echo "Missing required path: $path" >&2; exit 1; }
done
id "$APP_USER" >/dev/null 2>&1 || { echo "User does not exist: $APP_USER" >&2; exit 1; }
getent group "$APP_GROUP" >/dev/null 2>&1 || { echo "Group does not exist: $APP_GROUP" >&2; exit 1; }

if command -v apt-get >/dev/null 2>&1; then
  apt-get update
  apt-get install -y python3 python3-venv python3-pip python3-dev ffmpeg \
    build-essential git libjpeg-dev zlib1g-dev
else
  echo "This installer currently supports Raspberry Pi OS/Debian (apt-get)." >&2
  exit 1
fi

if [[ ! -e "$APP_DIR/config.json" ]]; then
  install -o "$APP_USER" -g "$APP_GROUP" -m 0640 \
    "$APP_DIR/config.example.json" "$APP_DIR/config.json"
fi
install -d -o "$APP_USER" -g "$APP_GROUP" -m 0700 \
  "$APP_DIR/run" "$APP_DIR/cache" "$APP_DIR/logs"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  sudo -u "$APP_USER" python3 -m venv "$VENV_DIR"
fi
sudo -u "$APP_USER" "$VENV_DIR/bin/python" -m pip install --upgrade pip
sudo -u "$APP_USER" "$VENV_DIR/bin/python" -m pip install -r "$APP_DIR/requirements-pi.txt"
sudo -u "$APP_USER" "$APP_DIR/scripts/build_spectrum_analyzer.sh"

if [[ ! -s "$APP_DIR/run/admin.token" ]]; then
  umask 077
  "$VENV_DIR/bin/python" -c 'import secrets; print(secrets.token_urlsafe(32))' \
    > "$APP_DIR/run/admin.token"
  chown "$APP_USER:$APP_GROUP" "$APP_DIR/run/admin.token"
  chmod 0600 "$APP_DIR/run/admin.token"
fi

render_unit() {
  local src="$1" dest="$2"
  sed \
    -e "s|@APP_USER@|$APP_USER|g" \
    -e "s|@APP_GROUP@|$APP_GROUP|g" \
    -e "s|@APP_DIR@|$APP_DIR|g" \
    -e "s|@VENV_DIR@|$VENV_DIR|g" \
    -e "s|@ADMIN_BIND@|$ADMIN_BIND|g" \
    "$src" > "$dest"
  chmod 0644 "$dest"
}

render_unit "$APP_DIR/systemd/p2278e-display.service" \
  "$UNIT_DIR/p2278e-display.service"
render_unit "$APP_DIR/systemd/p2278e-admin.service" \
  "$UNIT_DIR/p2278e-admin.service"

systemctl daemon-reload
systemctl enable --now p2278e-display.service p2278e-admin.service
systemctl is-active --quiet p2278e-display.service
systemctl is-active --quiet p2278e-admin.service

host="$(hostname -f 2>/dev/null || hostname)"
printf 'Installed P2278E appliance from %s as %s.\n' "$APP_DIR" "$APP_USER"
printf 'Display: %s | Admin: %s\n' \
  "$(systemctl is-active p2278e-display.service)" \
  "$(systemctl is-active p2278e-admin.service)"
if [[ "$ADMIN_BIND" == "127.0.0.1" || "$ADMIN_BIND" == "::1" ]]; then
  printf 'Admin is loopback-only (safe default). SSH tunnel example:\n'
  printf '  ssh -L 8787:127.0.0.1:8787 %s@%s\n' "$APP_USER" "$host"
  printf '  then open http://127.0.0.1:8787/ locally\n'
else
  printf 'Admin is LAN-exposed at http://%s:8787/; GET status is readable on your LAN.\n' "$host"
fi
printf 'Retrieve the mutation token locally with:\n'
printf '  sudo cat %s/run/admin.token\n' "$APP_DIR"
