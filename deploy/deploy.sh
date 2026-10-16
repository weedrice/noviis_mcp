#!/usr/bin/env bash
set -euo pipefail

export APP_DIR="${APP_DIR:?APP_DIR is required}"
export SERVICE_NAME="${SERVICE_NAME:-noviis-mcp.service}"
export ARCHIVE_PATH="${ARCHIVE_PATH:-/tmp/noviis-mcp-release.tgz}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
HELPER="$(mktemp)"
trap 'rm -f -- "$HELPER"' EXIT

# Read only the helper before the active release is changed.
tar -xOzf "$ARCHIVE_PATH" ./deploy/deploy_release.py > "$HELPER"
"$PYTHON_BIN" "$HELPER"
