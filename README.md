# News Agent

News Agent is an open-source investment-signal aggregator. It collects RSS feeds and built-in sources, deduplicates items with URL rules and application-level BM25, scores them with an LLM, and delivers personalized digests to Feishu, Discord, Telegram, or a custom endpoint.

The product is an **investment researcher**, not a news editor: every signal is expected to carry a sector, related assets, a direction, and a material change worth acting on. See [System Architecture](docs/system-architecture.md) for the implementation contract and its linked architecture documents.

## Runtime modes

There are exactly two modes:

| Mode | Purpose | Database |
|---|---|---|
| `slaver` | Runs locally, collects private sources, and delivers local digests. It can also pull the shared pool from a master and report local-only signals. | SQLite by default |
| `master` | Collects shared sources, serves the public portal and backoffice, and accepts slaver reports over MCP. | PostgreSQL |

Whether a slaver connects to a master is a configuration detail, not a third mode: an empty `server_url` means a fully independent local instance.

## Features

- Collect RSS, Feed pages, Hacker News, Google News, GitHub Trending, and community sources.
- Deduplicate by normalized URL and application-level BM25 before scoring, then re-check before persistence.
- Score investment relevance, assign sectors, extract related assets, and classify direction.
- Translate display fields for bilingual portals while keeping `assets` and `sectors` stable.
- Sync the shared signal pool between master and connected slavers; report through Streamable HTTP MCP.
- Show reporter aliases and reputation (overall and last-10 high-score rates) with complete public history; keep `score < 60` rows hidden and cap each reporter at 100 LLM-scored items per day.
- Browse the public portal, manage sources and preferences, and operate a loopback-only local console.

## Requirements

- macOS, Windows, or Linux
- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- An API key for a supported OpenAI-compatible or Anthropic-compatible LLM endpoint

News Agent is not yet published as a self-contained PyPI package: `resources/`, `prompts/`, and `alembic/` live beside the source tree. Run it from a checkout or use the installer.

## Install

macOS or Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/huawolf/news-agent/main/scripts/install.sh | bash
```

Windows PowerShell:

```powershell
iwr -useb https://raw.githubusercontent.com/huawolf/news-agent/main/scripts/install.ps1 | iex
```

From a clone, run `./scripts/install.sh` or `powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1`. The installer creates missing `config.json` and `.env` files from the examples, installs locked dependencies, registers a per-user service, and waits for the default local slaver health check at <http://127.0.0.1:12301/api/status>.

Review both files before the first fetch. The default configuration is an independent slaver with SQLite and does not require manai-pic, a master, or PostgreSQL. It still needs network access to its enabled sources and an LLM endpoint for scoring; delivery credentials are needed only for enabled push targets, and BrowserWorker is needed only for Feed-page collection.

## Configure

Use `config.json` for runtime behavior. Keep secrets in `.env`; the variable named by `llm.apiKeyName` must match the key stored there.

| Section | Purpose |
|---|---|
| `mode`, `server_url` | Select `master` or `slaver`; an empty `server_url` keeps a slaver local-only. |
| `database`, `auth` | Database DSN/schema and optional manai-pic identity lookup. `auth` is unused by a standalone slaver. |
| `schedule`, `fetch`, `filter` | Fetch cadence, concurrency/timeouts, and storage-age/score floors. |
| `dedup`, `ranking`, `tokenizer` | URL/BM25 deduplication, final-score weighting, and text tokenization. |
| `output_language`, `content_languages`, `translation` | Primary output language, additional portal languages, and the master-only translation worker. |
| `llm` | Scoring endpoint, model, protocol, prompts, retries, and batch limits. |
| `delivery`, `push` | Cron push schedules, section limits, and Feishu/Discord/Telegram/custom targets. |
| `sync`, `report`, `mcp` | Shared-pool pull window, MCP reporting limits/quotas, and server transport settings. |
| `log` | Log retention. |

Start from [config.json.example](config.json.example) and [.env.example](.env.example); the example config is complete and the environment file lists every supported secret. Never commit the active files.

### Environment variables

| Variable | Purpose |
|---|---|
| `NEWS_AGENT_CONFIG` | Override the configuration file path. |
| `NEWS_AGENT_DATA_DIR` | Store the SQLite database and logs outside the project tree. |
| `NEWS_AGENT_DATABASE_URL` | Override `database.url`, including for Alembic. |
| `NEWS_AGENT_MEMBERSHIP_DATABASE_URL` | Optional manai-pic membership lookup DSN. |
| `NEWS_AGENT_ADMINS` | Phone numbers or emails allowed to administer a master; comma-separated or JSON array. |
| `RSSHUB_BASE` | Override the RSSHub base URL for RSSHub-backed sources. |
| `BrowserWorker_PORT` / `BrowserWorker_LOCK_FILE` | Point Feed-page collection at an existing BrowserWorker daemon. |

Model keys, Feishu/Discord webhook URLs, bot tokens, `GITHUB_TOKEN`, `JINA_API_KEY`, and `REPORTER_KEY` are documented in [.env.example](.env.example). Never commit the active `.env`.

### Master identity

A master verifies manai-pic JWTs for sync, MCP reporting, and administration. Set `auth.main_url` to the identity service and `NEWS_AGENT_ADMINS` to the administrator allowlist. A connected slaver also needs a verified identity; a local-only slaver does not.

## Run

```bash
uv sync --locked
uv run news-agent check
uv run news-agent fetch
uv run news-agent push
uv run news-agent serve
```

`check` validates the mode, locale, and database settings, then prints the mode, the database URL with its password redacted, and the schema. It does not open a database connection or call the LLM.

`serve` starts the scheduler and the local web/API service on `127.0.0.1:12301` by default. Interactive API documentation is available at <http://127.0.0.1:12301/docs>.

Manage the per-user service with:

```bash
uv run news-agent service status
uv run news-agent service restart
```

Uninstall the service without removing its data with `scripts/uninstall.sh` or `scripts/uninstall.ps1`.

### Browser dependency

Only Feed-page collection needs an external BrowserWorker daemon (default port `12321`). RSS, RSSHub-backed signals, Hacker News, Google News, and GitHub Trending do not use a browser. If BrowserWorker is unavailable, the Feed section logs a failure and the rest of the fetch cycle continues.

## Deployment

The local service binds to loopback. A master must run behind a reverse proxy; [deploy/Caddyfile.example](deploy/Caddyfile.example) is one example that mounts the app under `/news` on `vaneworker.com`. It is not a required deployment shape. Keep FastAPI's `/docs`, `/redoc`, and `/openapi.json` restricted on a public master.

## Development

```bash
uv sync --locked
uv run ruff check src tests alembic
uv run pytest
uv build
```

Tests use temporary SQLite databases and do not require live LLM, delivery, or manai-pic credentials.

Contribution guidelines:

- Keep changes focused and preserve the master/slaver contract.
- Add regression tests for behavior changes and prefer one behavior per test.
- Update the relevant documentation when a config key, API contract, data model, or runtime behavior changes.
- Never commit `.env`, `config.json`, database files, logs, or credentials.
- Write code comments, public documentation, and prompts in English.

## Project layout

| Path | Contents |
|---|---|
| `src/` | Runtime package: API, adapters, pipeline, database, deduplication, delivery, and CLI. |
| `alembic/` | Versioned database migrations. Runs automatically before seed import at startup. |
| `resources/` | Default RSS/sector/adapter/feed seeds and portal assets. |
| `prompts/` | Scoring, translation, digest, and asset-recognition prompts. |
| `tests/pytest/` | Regression suite using temporary SQLite databases. |
| `scripts/` | macOS/Linux and Windows install/uninstall helpers. |
| `docs/` | Architecture, data model, pipelines, APIs, UI, integrations, and status documents. |

## Documentation

- [System architecture](docs/system-architecture.md)
- [Data model and seeds](docs/data-model-and-seeds.md)
- [Processing and sync](docs/processing-and-sync.md)
- [MCP and HTTP APIs](docs/mcp-and-http-api.md)
- [Web UI](docs/web-ui.md)
- [Integrations and status](docs/integrations-and-status.md)
- [Operator skill](SKILL.md)

## License

This project is licensed under the [MIT License](LICENSE).
