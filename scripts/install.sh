#!/usr/bin/env bash
# One-command installer for macOS and Linux.
set -euo pipefail

if [[ -f "pyproject.toml" && -d "src" ]]; then
    PROJECT_DIR="$(pwd)"
elif [[ -n "${BASH_SOURCE[0]:-}" && -f "$(dirname "${BASH_SOURCE[0]}")/../pyproject.toml" ]]; then
    PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
else
    PROJECT_DIR="${HOME}/.news-agent"
    echo "Cloning news-agent into ${PROJECT_DIR}..."
    if [[ -d "$PROJECT_DIR" ]]; then
        cd "$PROJECT_DIR"
        git pull --rebase || true
    else
        git clone https://github.com/huawolf/news-agent.git "$PROJECT_DIR"
    fi
fi
cd "$PROJECT_DIR"

ensure_uv() {
    if command -v uv >/dev/null 2>&1; then
        return 0
    fi
    if [[ -f "$HOME/.local/bin/uv" ]]; then
        export PATH="$HOME/.local/bin:$PATH"
        if command -v uv >/dev/null 2>&1; then
            return 0
        fi
    fi

    echo "uv was not found. Installing uv for the current user..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

    if ! command -v uv >/dev/null 2>&1; then
        echo "uv installation completed, but 'uv' command was not found on PATH."
        echo "Please restart your shell session or add \$HOME/.local/bin to your PATH, then rerun this script."
        exit 1
    fi
}

ensure_uv

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
