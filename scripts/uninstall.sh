#!/usr/bin/env bash
# Remove the per-user local service without deleting configuration or news data.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is required to run the uninstaller."
    exit 1
fi

uv run --no-sync python -m src.main service uninstall
echo "News Agent service removed. User configuration and news data were kept."
