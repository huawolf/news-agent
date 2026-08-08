---
name: news-agent-operator
description: Install, configure, upgrade, operate, and troubleshoot the News Agent repository on macOS, Linux, or Windows without using a browser. Use when an AI agent must set up the local service, write LLM or delivery secrets to .env, configure model endpoints and protocols through the local API, manage schedules and preferences, test connectivity, run fetch or preview jobs, perform a confirmed push, inspect status or logs, or recover an existing installation.
---

# News Agent Operator

Operate News Agent from a local shell. Keep secrets in `.env`, use the validated
local API for supported configuration changes, and verify every state-changing
step from machine-readable responses.

## Guardrails

- Run management commands on the same machine as News Agent. Regular clients
  bind their control API to `127.0.0.1:12301`. A designated mix server may bind
  externally for shared-news access, but its management routes must use a
  private `NEWS_AGENT_LOCAL_TOKEN`.
- Ask for missing product choices. Never invent a model, endpoint, credential,
  delivery destination, timezone, schedule, language, or ranking preference.
- Treat API keys, webhook URLs, and `NEWS_AGENT_LOCAL_TOKEN` as secrets. Never
  print them, include them in logs or final responses, or commit `.env`.
- Do not confuse authentication domains. `processednews` is the non-private
  shared-news API value; `NEWS_AGENT_LOCAL_TOKEN` is the optional private
  password for the local configuration and job-control API.
- Preserve unrelated `.env` entries. Use a structured file-editing operation;
  do not rebuild the file with `echo`, shell interpolation, or a broad rewrite.
- Do not edit `config.json` directly when a local API endpoint exists. API writes
  are validated, locked, audited, and atomic.
- Do not send a real digest until the user explicitly confirms the destination
  and asks to send. Prefer `preview` during setup.
- Do not claim success from installer output alone. Require health, saved-config,
  and model-test checks.

## 1. Resolve The Installation

If the current directory contains `pyproject.toml` and `src/`, use it. Otherwise
check the default one-command installation directory:

- macOS/Linux: `$HOME/.news-agent`
- Windows: `$env:USERPROFILE\.news-agent`

Validate the directory before operating on it. Do not search unrelated home
directories or create a second clone when a valid installation already exists.

Use `http://127.0.0.1:12301` as the API base URL unless the user provides an
explicit local override.

## 2. Install Or Upgrade

For macOS or Linux, run the official one-command installer when no source tree
is available:

```bash
curl -fsSL https://raw.githubusercontent.com/huawolf/news-agent/main/scripts/install.sh | bash
```

For Windows PowerShell:

```powershell
iwr -useb https://raw.githubusercontent.com/huawolf/news-agent/main/scripts/install.ps1 | iex
```

When already inside a clone, run `./scripts/install.sh` on macOS/Linux or
`powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1` on Windows.
The installer syncs locked runtime dependencies, installs the per-user service,
terminates any old service process, and starts the updated service.

After installation, resolve the actual project directory again before running
`uv run` commands.

## 3. Collect Required Settings

Regular user installations use the default client connection unless the user
is explicitly operating the shared server:

```json
"mode_settings": {
  "mode": "client",
  "server_url": "http://13.158.182.33:12301",
  "server_api_token_name": "processednews"
}
```

Use `mix` only for the shared server operator. Use `standalone` only for an
explicit self-contained deployment.
Client mode retrieves shared entries with `score >= 60` and locally fetches/scores only
custom RSS feeds absent from the server catalog. Manual and scheduled runs use
the same pipeline: one unified news pool plus the server-provided GitHub digest.

When starting a mix server for remote clients, bind it to an appropriate
network interface, for example `news-agent serve --host 0.0.0.0 --port 12301`,
and configure `NEWS_AGENT_LOCAL_TOKEN` before exposing the port. The public
`processednews` value grants shared-news reads only and must not grant local
configuration or job-control access.

Obtain these values before configuring:

1. LLM model name, endpoint, API-key environment variable name, and API key.
2. Desired protocol, or permission to infer it.
3. Output language: `zh` or `en`.
4. Natural-language ranking preferences.
5. Delivery destination and its webhook or token.
6. IANA timezone, cron schedules, and `max_items` for each delivery.

Explain that `max_items` is the final news delivery count. The unified digest stage ranks
all eligible fresh entries, gives up to `max_items * 3` leading candidates to the
LLM, and asks it to compose at most `max_items` items.

## 4. Write Secrets To `.env`

Edit `<project>/.env` directly and preserve existing values. Typical variables:

```dotenv
DEEPSEEK_API_KEY=...
FEISHU_WEBHOOK_URL=...
# DISCORD_WEBHOOK_URL=...
# CUSTOM_PUSH_URL=...
# CUSTOM_PUSH_TOKEN=...
# NEWS_AGENT_LOCAL_TOKEN=...
```

The LLM variable name is not fixed. It must exactly match `llm.apiKeyName`, for
example `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or a provider-specific name.

On macOS/Linux, restrict a newly created or exposed secret file with:

```bash
chmod 600 .env
```

Restart after direct `.env` edits so the service process receives new values:

```bash
uv run news-agent service restart
```

On Windows, run the same command through `uv` from PowerShell. Never place a
secret literally in a process argument when a private file-editing tool is
available.

If `NEWS_AGENT_LOCAL_TOKEN` is set, include it as `X-News-Agent-Token` on every
subsequent local management API request. Load it privately from `.env`; do not
display it. Do not use it as the client connection value: shared-news requests
use `processednews` from `mode_settings.server_api_token_name`.

## 5. Verify Service Health

Request `GET /api/status`. Require HTTP 200 and JSON with
`"status": "running"`. Record `config_path` and confirm it belongs to the
resolved installation.

If health fails, inspect:

```bash
uv run news-agent service status
```

Then inspect `<project>/logs/YYYYMMDD/app.log` and `web.log`. On Linux, also use
`systemctl --user status news-agent.service --no-pager`. Do not expose secrets
while reporting errors.

## 6. Configure The LLM

Infer the protocol in this order unless the user explicitly selects one:

1. Endpoint ending in `/chat/completions` -> `openai_chat`.
2. Endpoint ending in `/responses` or `/response` -> `openai_responses`.
3. Endpoint ending in `/messages` -> `anthropic_messages`.
4. Model containing `claude`, `opus`, `sonnet`, or `haiku` ->
   `anthropic_messages`.
5. GPT, ChatGPT, `o1`, `o3`, or `o4` model -> `openai_responses`.
6. Otherwise -> `openai_chat`.

Base URLs and complete endpoint URLs are both valid. The request layer appends
or replaces the protocol suffix without duplicating it.

Persist the selection with `PUT /api/llm-settings`:

```json
{
  "model": "USER_MODEL",
  "base_url": "USER_ENDPOINT",
  "protocol": "openai_chat",
  "api_key_name": "USER_API_KEY_VARIABLE"
}
```

Require HTTP 200. Then request `GET /api/config` and verify that `llm.model`,
`llm.baseUrl`, `llm.protocol`, and `llm.apiKeyName` exactly match the intended
values. Never silently replace a custom endpoint with a provider default.

Test with `POST /api/llm/test` using the same model, endpoint, protocol, and key
variable name. Omit `api_key` so the server reads the secret from its process
environment. Require HTTP 200, `"ok": true`, and inspect the returned normalized
`endpoint`. A failed test is not permission to change provider or model.

## 7. Configure Preferences And Delivery

Set output language with `PUT /api/language`:

```json
{"language": "zh"}
```

Set the user's natural-language ranking request with
`PUT /api/personal-preferences`:

```json
{"description": "USER_PREFERENCE_TEXT"}
```

Set schedules with `PUT /api/delivery`. Use five-field cron expressions and an
IANA timezone. Read the current configuration first. Omit `delivery.immediate`
to preserve it unless the user asks to change it.

```json
{
  "value": {
    "timezone": "Asia/Shanghai",
    "schedules": [
      {
        "id": "delivery-1",
        "cron": "0 10 * * *",
        "max_items": 5
      }
    ]
  }
}
```

After every mutation, require HTTP 200 and read `GET /api/config` to verify the
saved values. Configuration changes hot-reload; only direct `.env` edits require
a service restart.

Feishu is enabled in the default configuration. Setting
`FEISHU_WEBHOOK_URL` configures its destination. Do not enable Discord or custom
delivery by editing `config.json` without explicit user approval; this release
does not expose a dedicated channel-enable API.

## 8. Validate Without Sending

Start a fetch with `POST /api/jobs/fetch`. Save the returned job `id`, then poll
`GET /api/jobs/{id}` until `status` is `succeeded`, `failed`, or `cancelled`.
Treat `failed` as failure and report its sanitized `error`.

After a successful fetch, start `POST /api/jobs/preview` and poll it the same
way. Inspect the preview content and confirm the item count, language, relevance,
and absence of secrets. Preview does not send or mark links as sent.

Only after explicit user confirmation, start `POST /api/jobs/push` and poll the
job to completion. Reconfirm the destination immediately before this call.

Use `POST /api/jobs/run?confirm=true` only when the user explicitly requests one
combined fetch-and-send operation. Never infer confirmation from an earlier
configuration request.

For a mix server, a successful full run must update the shared news pool and
attempt GitHub generation regardless of the run time. Verify `app.log` contains
`GH:` progress, inspect `news-data/github-latest.md`, and require
`GET /api/server/github-trending` to return non-empty `github` before declaring
the shared GitHub service healthy. Delivery schedule objects must contain only
`id`, `cron`, and `max_items`; do not add legacy `sections` arrays.

## 9. MCP Handoff

For an MCP-capable local agent, configure stdio with the resolved project path:

```json
{
  "command": "uv",
  "args": ["run", "--no-sync", "news-agent", "mcp"],
  "cwd": "/absolute/path/to/news-agent"
}
```

Use MCP for source management, preferences, schedules, fetch, preview, confirmed
pushes, job status, and logs. MCP does not currently configure LLM credentials,
model endpoints, protocols, or delivery-channel enablement; complete initial
setup through `.env` and the local REST API first.

## Completion Report

Report only:

- resolved installation and configuration paths;
- service health;
- model, normalized endpoint, protocol, and successful/failed test status;
- output language, timezone, schedules, and final item limits;
- configured delivery channel names without secret values;
- fetch and preview job outcomes;
- whether a real push was skipped or explicitly confirmed and completed.

State any skipped validation or platform limitation. Never include credential or
webhook values.
