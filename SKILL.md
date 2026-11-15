---
name: news-agent-operator
description: Install, configure, operate, and troubleshoot News Agent on macOS, Linux, or Windows without using a browser. Use when an agent must manage the local service, configure model or delivery settings, run a fetch, inspect status, or recover an installation.
---

# News Agent Operator

Operate News Agent from the local shell. Keep secrets in `.env`, use the current CLI or validated local API for supported configuration changes, and verify state-changing operations from machine-readable responses.

## Guardrails

- Run management commands on the same machine as News Agent. The local service binds to `127.0.0.1:12301`; expose a master only through a configured reverse proxy.
- Ask about missing choices. Do not invent a model, endpoint, credential, delivery destination, timezone, schedule, output language, or ranking preference.
- Treat API keys and webhook URLs as secrets. Never print them, include them in logs or responses, or commit `.env`.
- Connected slavers, synchronization, MCP reporting, and master administration use verified manai-pic JWTs; a local-only slaver with an empty `server_url` does not require manai-pic.
- Preserve unrelated `.env` entries. Use a structured file-editing operation rather than rebuilding the file with shell interpolation.
- Do not send a real digest until the user explicitly confirms the destination and requests delivery.
- Do not claim success from installer output alone; verify service health and the requested operation.

## Resolve the Installation

If the current directory contains `pyproject.toml` and `src/`, use it. Otherwise check the default installation directory:

- macOS/Linux: `$HOME/.news-agent`
- Windows: `$env:USERPROFILE\.news-agent`

Validate the directory before operating on it. The local API base URL is `http://127.0.0.1:12301` unless the user specifies an override.

## Install or Upgrade

For a new installation, use the platform installer:

```bash
curl -fsSL https://raw.githubusercontent.com/huawolf/news-agent/main/scripts/install.sh | bash
```

```powershell
iwr -useb https://raw.githubusercontent.com/huawolf/news-agent/main/scripts/install.ps1 | iex
```

From an existing clone, run `./scripts/install.sh` on macOS/Linux or `powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1` on Windows. The installer syncs locked dependencies and installs or restarts the per-user service. It assumes the default slaver configuration and its health check uses `/api/status`; a master deployment must be started and verified separately.

There is no self-contained PyPI release yet: `resources/`, `prompts/`, and `alembic/` live beside the source tree. Operate from a checkout or the installer's `$HOME/.news-agent` directory.

## Configure

The default mode is a local slaver with SQLite. It runs without manai-pic, a master, or PostgreSQL while `server_url` is empty. It still needs network access to enabled sources and an LLM endpoint for scoring; delivery credentials are optional, and BrowserWorker is only needed for Feed pages. Set `server_url` to connect it to a master; a connected slaver then requires a verified manai-pic identity before synchronizing or reporting. Master mode requires PostgreSQL and verified manai-pic identities; master administration is restricted by the `NEWS_AGENT_ADMINS` allowlist.

Use `config.json` for model, source, schedule, and delivery settings. Store credentials in `.env` and ensure the LLM key variable matches `llm.apiKeyName`. Restart the service after editing `.env` directly:

```bash
uv run news-agent service restart
```

When a supported local API exists for a configuration change, prefer it over editing `config.json` directly.

## Operate and Verify

Run commands from the resolved project directory:

```bash
uv run news-agent check
uv run news-agent fetch
uv run news-agent push
uv run news-agent serve
uv run news-agent service status
```

`news-agent check` loads the configuration, validates the mode and DSN, and prints `mode`, a password-redacted `database`, and `schema`. It does not connect to the database or call the LLM. Use `GET /api/status` to verify a slaver responds with HTTP 200 and `{"status": "running", "mode": "slaver"}`; master does not expose that route. Check `logs/` for diagnostics; do not include secrets in reports. Use `news-agent fetch` to run a collection cycle and `news-agent push` only after confirming the destination and send request.

Only Feed-page collection needs an external BrowserWorker daemon (default port `12321`). RSS, RSSHub-backed signals, Hacker News, Google News, and GitHub Trending do not use a browser.

## Report

Summarize the installation/configuration paths, service health, configuration-check result, schedules, delivery channel names, and requested operation results. Include an LLM connectivity result only if the user explicitly asked for a test. State any skipped validation or platform limitation. Never include credential or webhook values.
