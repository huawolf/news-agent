#!/usr/bin/env bash
# One-command installer for macOS and Linux.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is required. Install it from https://docs.astral.sh/uv/ and rerun this script."
    exit 1
fi

if [[ ! -f .env ]]; then
    cp .env.example .env
    echo "Created .env from .env.example. Add your LLM and push credentials before the first fetch."
fi

echo "Syncing locked runtime dependencies..."
uv sync --locked --no-dev

echo "Installing the per-user local service..."
uv run --no-sync python -m src.main service install

echo "Starting the local service..."
uv run --no-sync python -m src.main service start

echo "News Agent is ready at http://127.0.0.1:12301"
