# News Agent

News Agent is a local, personal news service. It collects RSS feeds, GitHub Trending, and Hacker News, uses an OpenAI-compatible LLM to rank and summarize items, and delivers digests to Feishu or Discord.

It runs on macOS, Windows, and Linux. A local web console, HTTP API, and stdio MCP server all manage the same configuration and jobs.

![News Agent overview](resources/description.jpg)

## Features

- Aggregate RSS feeds, GitHub Trending, and Hacker News.
- Rank, filter, deduplicate, and summarize content with an LLM.
- Add, update, verify, and remove RSS sources from the web console, API, or MCP.
- Set interests, exclusions, source weights, delivery times, and item limits.
- Run scheduled fetches and deliveries, or trigger them manually.
- Deliver to Feishu, Discord, or a custom HTTP endpoint.
- Keep configuration, logs, job history, and backups in the local user-data directory.
- Bind the local control plane to `127.0.0.1:12301` by default.

## Requirements

- macOS, Windows, or Linux
- [uv](https://docs.astral.sh/uv/)
- An OpenAI-compatible LLM API key

The installer scripts for macOS, Linux, and Windows will automatically install `uv` for the current user if it is not already available.

## Install

Clone the repository and run the installer from its root directory.

macOS or Linux:

```bash
./scripts/install.sh
```

Windows PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

The installer creates `.env` from `.env.example`, installs the locked runtime dependencies, and registers a per-user login service. Open <http://127.0.0.1:12301> after installation.

## Configure

Add your credentials to `.env`, or edit them in the **Secrets and connection settings** section of the local web console. The console displays local `.env` values, so keep the browser session private. The names must match the `apiKeyName` fields in your configuration.

```dotenv
DEEPSEEK_API_KEY=your_llm_api_key
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/...
# DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

The first run creates the local configuration from [config.json.example](config.json.example). Use the web console to manage feeds, preferences, and schedules. For file-based configuration, copy the example and edit only the settings you need.

Important settings:

- `preferences`: interests, exclusions, source weights, language preference, and diversity limits.
- `delivery.schedules`: cron schedules, sections, and `max_items` per delivery.
- `delivery.immediate`: high-score alert threshold and daily limit.
- `push`: enable Feishu, Discord, or a custom endpoint.

## Run

Use `uv run news-agent` for all commands:

```bash
uv sync                         # Install development dependencies
uv run news-agent check         # Check LLM connectivity
uv run news-agent fetch         # Fetch and score once
uv run news-agent push          # Generate and send one digest
uv run news-agent serve         # Run the local web/API service
uv run news-agent mcp           # Run the stdio MCP server
uv run news-agent service status
```

The `serve` command starts the built-in scheduler and the local API at <http://127.0.0.1:12301>. Interactive API documentation is available at <http://127.0.0.1:12301/docs>.

To remove the login service without deleting data:

```bash
./scripts/uninstall.sh
```

On Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\uninstall.ps1
```

## Local API and MCP

The local API supports configuration, source, delivery, job, and log operations. Set `NEWS_AGENT_LOCAL_TOKEN` in `.env` to require the `X-News-Agent-Token` header for API requests.

The MCP server is intended for a local agent process:

```json
{
  "command": "uv",
  "args": ["run", "news-agent", "mcp"],
  "cwd": "/path/to/news-agent"
}
```

Available MCP tools include source management, preference and schedule updates, manual fetch/push jobs, digest previews, job status, and recent logs. `run_push` requires explicit confirmation.

## Data and Logs

Runtime data is stored in the project directory by default:

| Type | Directory |
| --- | --- |
| News data | `news-data/` |
| Logs | `logs/` |
| Job records | `runs/` |
| Config backups | `backups/` |

Use `NEWS_AGENT_DATA_DIR` or `NEWS_AGENT_CONFIG` to override these paths. Application logs use rotating files; the project does not depend on system journals.

## Development

```bash
uv sync
uv run python -m pytest tests/pytest -q
```

See [docs/README.md](docs/README.md) for the documentation index and [docs/system-architecture.md](docs/system-architecture.md) for the local control-plane and cross-platform design.

## Security

News Agent binds to loopback by default. Do not expose the local API to an untrusted network. Keep `.env`, webhook URLs, and API keys out of version control.

## Contributing

Issues and pull requests are welcome. Please keep changes focused, add tests for behavior changes, and update the relevant documentation when configuration or user-facing behavior changes.

## License

This project is licensed under the [MIT License](LICENSE).
