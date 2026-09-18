#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd -- "$APP_DIR"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  echo 'محیط اجرا آماده نیست؛ ابتدا bash install.sh را اجرا کنید.' >&2
  exit 1
fi
exec "$APP_DIR/.venv/bin/python" -u "$APP_DIR/bot.py"
