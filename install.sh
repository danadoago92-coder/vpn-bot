#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SYSTEMD=0
NO_SETUP=0
TARGET_DIR=""
SERVICE_NAME="vpn-bot"
usage() {
  cat <<'EOF'
Usage: bash install.sh [--systemd] [--target /opt/vpn-bot] [--service-name vpn-bot] [--no-setup]
Default: install a local virtual environment and open the Persian SSH settings menu.
--systemd: root-only; deploy source, configure a dedicated unprivileged service, start it.
--target: systemd installation folder. Existing .env, data and backups are retained.
--no-setup: preserve current settings and only verify them (useful for updates).
Python 3.11+ is required. The installer never resets customer data.
EOF
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --systemd) SYSTEMD=1; shift ;;
    --target) [[ $# -ge 2 ]] || { usage; exit 2; }; TARGET_DIR="$2"; shift 2 ;;
    --service-name) [[ $# -ge 2 ]] || { usage; exit 2; }; SERVICE_NAME="$2"; shift 2 ;;
    --no-setup) NO_SETUP=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done
[[ "$(uname -s)" == Linux ]] || { echo 'For non-Linux systems use the manual Python instructions in README.md.' >&2; exit 1; }
if [[ "$SYSTEMD" == 1 ]]; then
  [[ "$EUID" == 0 ]] || { echo 'Systemd installation needs root: sudo bash install.sh --systemd' >&2; exit 1; }
  command -v systemctl >/dev/null && [[ -d /run/systemd/system ]] || { echo 'A running systemd host is required. Use the local install instead.' >&2; exit 1; }
  [[ "$SERVICE_NAME" =~ ^[a-z][a-z0-9-]{0,25}$ ]] || { echo 'Invalid service name.' >&2; exit 1; }
  if [[ -z "$TARGET_DIR" ]]; then
    read -r -p 'Installation directory [/opt/vpn-bot]: ' TARGET_DIR
    TARGET_DIR="${TARGET_DIR:-/opt/vpn-bot}"
  fi
  [[ "$TARGET_DIR" =~ ^/[a-zA-Z0-9_./-]+$ && "$TARGET_DIR" != / && "$TARGET_DIR" != /root* ]] || { echo 'Choose a dedicated absolute path such as /opt/vpn-bot, outside /root and without spaces.' >&2; exit 1; }
else
  [[ -z "$TARGET_DIR" ]] || { echo '--target requires --systemd.' >&2; exit 2; }
  TARGET_DIR="$SOURCE_DIR"
fi
find_python() {
  local candidate
  for candidate in python3 python3.14 python3.13 python3.12 python3.11; do
    if command -v "$candidate" >/dev/null && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3,11))' 2>/dev/null; then
      PYTHON_BIN="$(command -v "$candidate")"
      return 0
    fi
  done
  return 1
}
install_prerequisites() {
  [[ "$EUID" == 0 ]] || { echo 'Install Python 3.11+, pip, venv and CA certificates using your server package manager, then retry.' >&2; return 1; }
  if command -v apt-get >/dev/null; then
    apt-get update
    apt-get install -y python3 python3-venv python3-pip ca-certificates
  elif command -v dnf >/dev/null; then
    dnf install -y python3 python3-pip ca-certificates
  elif command -v yum >/dev/null; then
    yum install -y python3 python3-pip ca-certificates
  else
    echo 'No supported package manager. Install Python 3.11+, pip, venv and CA certificates manually.' >&2
    return 1
  fi
}
if ! find_python; then
  install_prerequisites
  find_python || { echo 'Your distribution ships an older Python. Install Python 3.11+ ; the system Python will not be replaced.' >&2; exit 1; }
fi
if [[ "$SYSTEMD" == 1 ]]; then
  mkdir -p -- "$TARGET_DIR"
  TARGET_DIR="$(cd -- "$TARGET_DIR" && pwd -P)"
  if [[ "$TARGET_DIR" != "$SOURCE_DIR" ]]; then
    "$PYTHON_BIN" - "$SOURCE_DIR" "$TARGET_DIR" <<'PY'
from pathlib import Path
import os, shutil, sys
source, target = map(Path, sys.argv[1:])
if source in target.parents or target in source.parents:
    raise SystemExit('Source and target must be separate folders, not nested.')
excluded = {'.env', '.git', '.venv', 'venv', 'data', 'backups', 'admins.json', '__pycache__', '.pytest_cache', '.github'}
for path in source.rglob('*'):
    relative = path.relative_to(source)
    if any(part in excluded for part in relative.parts) or path.is_symlink():
        continue
    if path.name.startswith('.env') and path.name != '.env.example':
        continue
    if path.suffix in {'.db', '.sqlite', '.sqlite3', '.log', '.pyc', '.pem', '.key', '.lock'}:
        continue
    destination = target / relative
    if path.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
    elif path.is_file():
        shutil.copy2(path, destination)
if not (target / '.env').exists() and (source / '.env').is_file():
    shutil.copy2(source / '.env', target / '.env')
    os.chmod(target / '.env', 0o600)
if not (target / 'admins.json').exists() and (source / 'admins.json').is_file():
    shutil.copy2(source / 'admins.json', target / 'admins.json')
    os.chmod(target / 'admins.json', 0o600)
PY
  fi
fi
cd -- "$TARGET_DIR"
if [[ "$NO_SETUP" == 0 ]]; then
  "$PYTHON_BIN" "$TARGET_DIR/setup.py"
fi
"$PYTHON_BIN" "$TARGET_DIR/setup.py" --check
if [[ ! -x "$TARGET_DIR/.venv/bin/python" ]]; then
  if ! "$PYTHON_BIN" -m venv "$TARGET_DIR/.venv"; then
    echo 'Creating venv failed. Debian/Ubuntu: install python3-venv (or python3.11-venv for your Python version). Fedora/RHEL: install the matching python3-pip package.' >&2
    exit 1
  fi
fi
"$TARGET_DIR/.venv/bin/python" -c 'import sys; raise SystemExit(sys.version_info < (3,11))'
"$TARGET_DIR/.venv/bin/python" -m pip install --disable-pip-version-check -r "$TARGET_DIR/requirements.txt"
if [[ "$SYSTEMD" == 1 ]]; then
  if ! id "$SERVICE_NAME" >/dev/null 2>&1; then
    useradd --system --home-dir "$TARGET_DIR" --no-create-home --shell /usr/sbin/nologin "$SERVICE_NAME"
  fi
  [[ "$(id -u "$SERVICE_NAME")" != 0 ]] || { echo 'Service account must not be root.' >&2; exit 1; }
  DB_DIR="$("$PYTHON_BIN" -c 'from config import load_settings; print(load_settings().db_path.parent)')"
  [[ "$DB_DIR" == "$TARGET_DIR/data" || "$DB_DIR" == "$TARGET_DIR/data/"* ]] || { echo 'For the managed service, DB_PATH must be inside its data/ folder.' >&2; exit 1; }
  mkdir -p -- "$DB_DIR" "$TARGET_DIR/backups"
  chown -R root:"$SERVICE_NAME" -- "$TARGET_DIR"
  chmod 750 -- "$TARGET_DIR"
  chmod 640 -- "$TARGET_DIR/.env"
  chown "$SERVICE_NAME":"$SERVICE_NAME" -- "$TARGET_DIR/.env"
  chmod 600 -- "$TARGET_DIR/.env"
  chown "$SERVICE_NAME":"$SERVICE_NAME" -- "$TARGET_DIR/admins.json"
  chmod 600 -- "$TARGET_DIR/admins.json"
  # Existing data is kept and made accessible to the dedicated service account.
  chown -R "$SERVICE_NAME":"$SERVICE_NAME" -- "$TARGET_DIR/data" "$TARGET_DIR/backups"
  chmod 700 -- "$TARGET_DIR/data" "$TARGET_DIR/backups"
  # Source files/venv are read-only to the service account; permit group traversal/read.
  find "$TARGET_DIR" -path "$TARGET_DIR/data" -prune -o -path "$TARGET_DIR/backups" -prune -o -type d -exec chmod g+rx {} +
  find "$TARGET_DIR" -path "$TARGET_DIR/data" -prune -o -path "$TARGET_DIR/backups" -prune -o -name .env -prune -o -name admins.json -prune -o -type f -exec chmod g+r {} +
  cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Telegram Config Shop
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
User=$SERVICE_NAME
Group=$SERVICE_NAME
WorkingDirectory=$TARGET_DIR
ExecStart=$TARGET_DIR/.venv/bin/python -u $TARGET_DIR/bot.py
Environment=PYTHONDONTWRITEBYTECODE=1
Restart=on-failure
RestartSec=10
TimeoutStopSec=35
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$TARGET_DIR/data

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME.service"
  systemctl restart "$SERVICE_NAME.service"
  echo "Installed. Check: systemctl status $SERVICE_NAME.service"
  echo "Logs: journalctl -u $SERVICE_NAME.service -n 50 --no-pager"
else
  echo 'آماده است. برای اجرا: bash run.sh'
fi
