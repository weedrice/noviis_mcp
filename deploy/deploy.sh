#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:?APP_DIR is required}"
SERVICE_NAME="${SERVICE_NAME:-noviis-mcp.service}"
ARCHIVE_PATH="${ARCHIVE_PATH:-/tmp/noviis-mcp-release.tgz}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-$APP_DIR/.venv}"
ENV_FILE="${ENV_FILE:-$APP_DIR/.env}"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

mkdir -p "$APP_DIR"

if [[ ! -f "$ARCHIVE_PATH" ]]; then
  echo "Archive not found: $ARCHIVE_PATH" >&2
  exit 1
fi

tar -xzf "$ARCHIVE_PATH" -C "$TMP_DIR"

if [[ -d "$APP_DIR/.git" ]]; then
  find "$APP_DIR" -mindepth 1 -maxdepth 1 \
    ! -name ".env" \
    ! -name ".env.local" \
    ! -name ".venv" \
    -exec rm -rf {} +
else
  find "$APP_DIR" -mindepth 1 -maxdepth 1 \
    ! -name ".env" \
    ! -name ".env.local" \
    ! -name ".venv" \
    -exec rm -rf {} +
fi

find "$TMP_DIR" -mindepth 1 -maxdepth 1 \
  ! -name ".env" \
  ! -name ".env.local" \
  -exec cp -a {} "$APP_DIR"/ \;

if [[ ! -d "$VENV_DIR" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

if ! "$VENV_DIR/bin/python" -m pip --version >/dev/null 2>&1; then
  "$VENV_DIR/bin/python" -m ensurepip --upgrade
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install "$APP_DIR"

if command -v sudo >/dev/null 2>&1; then
  sudo systemctl daemon-reload
  sudo systemctl restart "$SERVICE_NAME"
  sudo systemctl status "$SERVICE_NAME" --no-pager
else
  systemctl daemon-reload
  systemctl restart "$SERVICE_NAME"
  systemctl status "$SERVICE_NAME" --no-pager
fi
