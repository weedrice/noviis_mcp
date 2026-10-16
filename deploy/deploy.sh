#!/usr/bin/env bash
set -euo pipefail

export APP_DIR="${APP_DIR:?APP_DIR is required}"
export SERVICE_NAME="${SERVICE_NAME:-noviis-mcp.service}"
export ARCHIVE_PATH="${ARCHIVE_PATH:-/tmp/noviis-mcp-release.tgz}"
ARCHIVE_SHA256="${ARCHIVE_SHA256:?ARCHIVE_SHA256 from the verified CI artifact is required}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
[[ "$ARCHIVE_SHA256" =~ ^[0-9a-f]{64}$ ]]
printf '%s  %s\n' "$ARCHIVE_SHA256" "$ARCHIVE_PATH" | sha256sum --check --status
HELPER_DIR="$(mktemp -d)"
trap 'rm -f -- "$HELPER_DIR/deploy_release.py" "$HELPER_DIR/release_artifact.py"; rmdir -- "$HELPER_DIR"' EXIT

# Execute helpers only after the complete archive matches the CI checksum.
tar -xOzf "$ARCHIVE_PATH" ./deploy/deploy_release.py > "$HELPER_DIR/deploy_release.py"
tar -xOzf "$ARCHIVE_PATH" ./deploy/release_artifact.py > "$HELPER_DIR/release_artifact.py"
PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" "$HELPER_DIR/deploy_release.py"
