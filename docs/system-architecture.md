# News Agent Architecture & Technical Specification

> **Status:** Core control plane implemented. The codebase provides a local Web/API server, stdio MCP server, in-application scheduler, file-based logging, and cross-platform service lifecycle installers. Native installation and service recovery across macOS, Windows, and Linux require validation on target environments. Legacy Linux/systemd details are archived in [archive/tech-spec-systemd.md](archive/tech-spec-systemd.md).
>
> **Goal:** Evolve News Agent from a single-machine Linux script into a local, cross-platform personal intelligence digest service for macOS, Windows, and Linux. Both human users (via a local Web UI) and AI agents (via Model Context Protocol) manage the same configuration, validation logic, background jobs, and audit logs.

Refer to [roadmap.md](roadmap.md) for milestone execution order and pending tasks.

---

## 1. Scope and Design Principles

### 1.1 Objectives

- **Single-User Local Service:** Operates locally on a single machine without user authentication, multi-tenancy, cloud control planes, or external database dependencies.
- **Unified Source & Delivery Management:** Both users and AI agents can add/manage RSS sources, configure ranking preferences, item limits, delivery schedules, and notification channels.
- **Dual Control Interfaces:** Provides a browser-based local Web console and a Python stdio MCP server.
- **Flexible Execution Engine:** Supports cron-scheduled execution as well as instant manual/Agent execution for fetching, previewing, and pushing digests.
- **Cross-Platform Consistency:** Ensures identical configuration, logging, API semantics, and job execution behaviors across macOS, Windows, and Linux.
- **Standard File Logging:** Uses application-level rolling log files instead of platform-dependent system journals.

### 1.2 Non-Goals

- No multi-tenant isolation, user accounts, or team collaboration features.
- No public or LAN exposure of local control plane APIs.
- Generic user-added sources are RSS/Atom feeds. Non-RSS integrations are handled as built-in signal adapters under `sections.signals`.

### 1.3 Core Principles

- **Single Source of Truth:** `config.json` is the sole source of truth for user preferences; `.env` or OS credential stores manage API keys and secrets only.
- **Unified Business Service Layer:** Web UI, REST API, and MCP must not mutate JSON files directly; all mutations must route through a shared core service layer.
- **Single Execution Engine:** All scheduled, manual, and Agent-triggered tasks route through a unified Job Executor to enforce mutual exclusion and prevent duplicate fetches or pushes.
- **Minimal OS Integration Boundary:** OS-level service managers (`launchd`, Windows Task Scheduler, `systemd`) are responsible solely for starting the local daemon on user login; business logic scheduling remains internal.

---

## 2. Target System Architecture

```text
Browser Web UI                  MCP Client / Agent
       |                                |
       +----------- Local API ----------+
                         |
           Config / Source / Job Service Layer
                  |              |
                  |              +-- Scheduler (APScheduler)
                  |
           Config Files, Logs, Run Records
                         |
             Fetch -> Score -> Rank -> Push
                         |
              Discord / Feishu / Custom Webhook
```

The local daemon binds to `http://127.0.0.1:12301` by default (`123001` exceeds the TCP/UDP port limit of `65535`).

### Component Responsibilities

- **Web UI:** Web-based control panel for feeds, preferences, delivery schedules, job statuses, logs, and historical runs.
- **Local API:** REST API powering the Web UI and local automation tools.
- **MCP Server:** Stdio-based MCP transport invoking the shared service layer.
- **Scheduler:** In-process `APScheduler` handling cron and interval triggers.
- **Job Executor:** Handles `fetch`, `push`, and `preview` execution with mutex locks, run states, and result persistence.
- **Config Service:** Schema validation, atomic reads/writes, and notifying the Scheduler of changes.
- **Logging & Audit Service:** Manages rolling log files and audits configuration modifications made by users or agents.
- **LLM Alert Policy:** Scoring mismatches, malformed responses, and other processing errors remain in local logs; only LLM connection failures and timeouts trigger Feishu error alerts.

---

## 3. Configuration & Data Directory Layout

### 3.1 Directory Structure

Runtime files are saved in the project root directory by default (configurable via `NEWS_AGENT_DATA_DIR`):

```text
news-agent/
  config.json
  .env
  news-data/
  logs/
    app.log
    fetch.log
    push.log
    web.log
    mcp.log
    audit.log
  runs/
```

- `news-data/`: Retains existing raw payload formats (`fetch-*.json`, `push-*.md`, `notify-*.md`).
- `runs/`: Stores structured JSON job execution summaries and statuses.

### 3.2 Configuration Schema Evolution

Top-level configuration schema with backward compatibility:

```json
{
  "personal_preferences": "Prioritize AI agents, model releases, and practical developer tools. Prefer Chinese summaries.",
  "llm": {
    "model": "deepseek-v4-flash",
    "baseUrl": "https://api.deepseek.com",
    "protocol": "openai_chat",
    "apiKeyName": "DEEPSEEK_API_KEY"
  },
  "delivery": {
    "timezone": "Asia/Shanghai",
    "schedules": [
      {
        "id": "morning",
        "cron": "0 10 * * *",
        "max_items": 10,
        "sections": ["rss", "github", "hackernews", "insights"]
      },
      {
        "id": "evening",
        "cron": "0 20 * * *",
        "max_items": 10,
        "sections": ["rss"]
      }
    ],
    "immediate": {"enabled": true, "threshold": 92, "daily_limit": 3}
  }
}
```

`llm.protocol` supports `openai_chat`, `openai_responses`, and
`anthropic_messages`. Protocol detection first examines the endpoint path:
`/chat/completions`, `/responses` (or `/response`), and `/messages` take
precedence. If the path is only a base URL, Claude family names (`claude`,
`opus`, `sonnet`, or `haiku`) select Anthropic Messages, while OpenAI model
names (`gpt`, `chatgpt`, `o1`, `o3`, or `o4`) select OpenAI Responses. Unknown
models default to OpenAI Chat Completions for compatibility. The detected value
is shown in the Web console and can be overridden before saving. Both base URLs
and complete endpoint URLs are accepted; the request layer normalizes them
without appending a duplicate protocol path.

Legacy `schedule.push_cron` settings are automatically migrated to `delivery.schedules` upon initial load.
When neither modern nor legacy delivery schedules are configured, the system
defaults to daily deliveries at 10:00 and 20:00 in `delivery.timezone`, with a
maximum of 10 news items per delivery. Explicit schedules, including an empty
schedule list used to disable automatic delivery, are preserved.

`delivery.schedules[].max_items` caps one combined headline list containing RSS
and Hacker News items (10 by default). Hacker News is not exposed as a separate
delivery or Web UI section. GitHub is independently capped by
`sections.github_trending.max_items` (3 by default) and starts numbering at 1.
Each item consists of a title and concise core summary. Metadata and the insights
section are used for the card title/preview only and are never sent as body text.
The fixed delivery-title prefix follows the generated title language, preventing
mixed Chinese/English card titles when a model deviates from the requested language.

### 3.3 Mutation Safeguards & Concurrency Controls

- **Validation:** Pydantic models validate types, value ranges, URLs, cron expressions, and webhook configurations.
- **Atomic File Operations:** Uses inter-process file locks, temporary swap files, and atomic replaces to prevent corruption during concurrent edits.
- **Versioning:** Increments configuration revision numbers on write.
- **Auditing:** Writes sanitized audit trails to `audit.log` capturing timestamps, invocation sources (`web`/`mcp`/`api`), and mutation summaries (excluding sensitive credentials).
- **Hot Reloading:** Notifies the in-process Scheduler to dynamically update jobs upon successful writes.

---

## 4. Feed Management & Ranking Pipeline

### 4.1 RSS Source Workflow

```text
Submit RSS URL
  -> Protocol & Schema Validation
  -> SSRF Prevention (Block localhost, RFC 1918 private IPs, intranet redirects)
  -> Network Verification, RSS/Atom Parsing & Entry Preview
  -> URL Deduplication
  -> Write to sources.add & Hot-reload Next Fetch Cycle
```

Feed sources track runtime metrics: enablement status, last successful fetch timestamp, consecutive failure counter, last error message, and auto-pause flags.

Every source is normalized to exactly one broad content category:

- `ai`: AI & Frontier Technology
- `developer_open_source`: Developers & Open Source
- `product_startup`: Products & Startups
- `business_investment`: Business & Investment
- `technology_policy`: Technology Industry & Policy
- `other`: General / Other

The Web UI localizes these stable IDs into Chinese or English. Its source list
shows only enabled sources used by the next fetch, supports category filtering,
and allows RSS/Atom URLs to be removed. Removing a bundled URL adds it to the
configuration block list; removing a user-added URL deletes it from `sources.add`.

### 4.1.1 24-Hour Publication Cutoff Filter
All fetched news entries (RSS, built-in signals, Hacker News, etc.) are subjected to a strict publication window (`fetch_lookback_minutes: 1440` by default). Entries with publication dates older than the configured lookback are automatically discarded before LLM scoring and persistence.

### 4.1.2 Built-In Signal Adapters

`sections.signals` feeds non-RSS and curated sources into the same entry contract used by RSS (`title`, `link`, `published`, `source`, `content`, `tags`, `score`, `summary`). Supported adapters include:

- GitHub Trending total, JavaScript, and Chinese variants via HTML scraping.
- Product Hunt via GraphQL when `PH_TOKEN` is configured.
- Reddit via no-key pullpush.io fallback, then Reddit public JSON fallback.
- V2EX create, share, and programmer nodes via public JSON API.
- App Store China, Taiwan, US, Japan, and Korea via Apple RSS JSON.
- 36Kr, Sspai, and OSChina via RSS.
- Jike AI Explore, AI Discussion, and Engineers topics via RSSHub.
- Google News `BUSINESS`, `TECHNOLOGY`, and `SCIENCE` topic RSS feeds for China
  (`zh-CN`, `CN:zh-Hans`) and the United States (`en-US`, `US:en`). Each of the
  six country/topic feeds contributes at most 20 entries per fetch.

These entries are scored, deduplicated, stored, and delivered through the existing fetch pipeline.

Google News uses a separate successful-fetch cursor for each country/topic feed,
stored in `news-data/google-news-state.json`. A feed requests entries published
after its own previous successful fetch; the initial and maximum recovery window
is 24 hours. Failed feeds do not advance their cursors. Topic entries retain the
country and topic tags and map to existing categories as follows:
`BUSINESS -> business_investment`, `TECHNOLOGY -> technology_policy`, and
`SCIENCE -> other`.

Signal adapters apply a pre-scoring quality gate before entries reach the LLM. Product Hunt, App Store, Reddit, V2EX, Jike, and domestic media adapters keep AI / LLM / Agent / model-driven application signals and drop obvious noise such as entertainment-only apps, generic local services, roundup posts, casual speculation, unrelated hardware financing, and stale items outside the lookback window. GitHub variants allow AI infrastructure and developer-tool opportunities, but still filter unrelated trending repositories.

### 4.2 Preference Prompting & Deterministic Re-ranking

Natural language preference string `personal_preferences` guides LLM scoring. The final ranking uses a deterministic scoring formula:

The scoring prompt treats three reader-value tracks as equal top priorities:
actionable public-equity investment signals, evidence-backed startup opportunities,
and major AI advances. High scores require concrete evidence such as earnings or
guidance changes, supply/demand and regulatory catalysts, user/revenue/retention
traction, validated capability gains, or material adoption. Topic labels, price
moves, financing announcements, and unverified AI claims do not qualify by
themselves.

```text
final_score = base_llm_score
            + interest_match_bonus
            + source_weight
            + recency_bonus
            - avoid_penalty
            - duplicate_penalty
```

Final RSS and Hacker News headlines are combined and truncated according to the
`max_items` parameter of the active schedule. GitHub is truncated independently.
Hacker News stories enter the same fetch, LLM scoring, ranking, and digest path as
all other news; the scheduled delivery does not run a second Hacker News summary.
The digest receives up to three times the target item count from the ranked pool.
Its prompt gives equal editorial priority to actionable public-equity news,
evidence-backed startup opportunities, and major AI/technology advances. When the
model returns fewer items than requested despite enough complete scored candidates,
the system fills the remaining slots deterministically from that ranked pool.

Sent history records only candidate URLs that occur in the final delivered Markdown.
Model inputs omitted from immediate or scheduled output remain eligible for a later
delivery for up to 24 hours; the previous push timestamp does not discard an unsent
candidate. Filter logs report score, sent-history, cutoff, diversity,
candidate-pool, and final-limit counts for each run.

### 4.3 GitHub Trending Deduplication & Permanent History Retention

GitHub repositories processed or sent by the system are recorded in `news-data/trending-history.json`. To enforce strict permanent deduplication ("historically sent repositories are never re-sent"), repository URLs are normalized (lowercased and stripped of trailing slashes) and entries are preserved indefinitely without time-based pruning (`keep_days` cleanup bypass).

---

## 5. Scheduling and Job Execution

### 5.1 In-Process Scheduling

Powered by `APScheduler`:
- **Interval Jobs:** Executes fetching cycles based on `fetch_interval_minutes`.
- **Cron Jobs:** Triggers delivery pipelines based on `delivery.schedules`.
- **Missed Run Policy:** Default policy skips missed scheduled executions during downtime, with an optional catch-up window.

### 5.2 Unified Job Executor

```text
Scheduler / Web / Local API / MCP
              -> submit(fetch | push | preview)
              -> Lock acquisition, Run record generation, Logging, Execution
```

- **Concurrency Control:** Mutex locks prevent concurrent runs of the same job type (returning active Job ID on collision).
- **Job Tracking:** Each job records `id`, `kind`, `source`, start/finish timestamps, execution status, and output summary.
- **Preview Dry-Run:** `preview` generates digest contents without updating item delivery histories or triggering notifications.

---

## 6. Local Web Console & REST API Specification

### Web UI Pages

1. **Headlines (default):** Latest generated delivery, opened as the first tab and loaded on page initialization.
2. **Settings:** Preferences, secret state indicators, schedule management, LLM
   protocol detection and override, an LLM connection test using the current
   form values, and contextual setup tooltips beside fields such as
   `FEISHU_WEBHOOK_URL`. The protocol selector and model API key share one
   responsive settings row for faster connection setup. Visible model and
   delivery connection fields are automatically persisted after a short input
   debounce; saves are serialized so an older request cannot overwrite newer
   form values. Starting a model test flushes any pending connection save first,
   and periodic status refreshes do not replace connection fields while they are
   focused, saving, or being tested. Optional integration
   and control-plane variables (`GITHUB_TOKEN`, `JINA_API_KEY`,
   `NEWS_AGENT_LOCAL_TOKEN`, and `PH_TOKEN`) remain supported through `.env`
   but are not rendered in this connection panel. Feishu and Discord webhook
   fields are rendered next to each other, with Discord immediately following
   Feishu.
3. **Sources:** The active fetch list, with bilingual content-category filtering, bulk RSS/Atom validation, and confirmed URL removal.
4. **Logs:** Real-time application log viewer (sanitized of secret keys).

### REST API Surface

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/status` | System status and service health |
| `GET` | `/api/config` | Fetch current configuration |
| `PUT` | `/api/config/preferences` | Update legacy structured preferences |
| `PUT` | `/api/personal-preferences` | Update natural language preference statement |
| `PUT` | `/api/llm-settings` | Save model, endpoint, and selected protocol |
| `POST` | `/api/llm/test` | Test the current model connection without persisting form values |
| `GET` | `/api/sources` | List configured feed sources |
| `POST` | `/api/sources` | Add single feed source |
| `POST` | `/api/sources/verify` | Validate single feed URL |
| `POST` | `/api/sources/bulk` | Bulk import feed sources |
| `GET` | `/api/news-sources` | Combined feed list (RSS + built-in) |
| `PATCH` | `/api/sources/{id}` | Update feed source state |
| `DELETE` | `/api/sources/{id}` | Remove feed source |
| `PUT` | `/api/delivery/schedules` | Update delivery schedules |
| `POST` | `/api/jobs/fetch` | Trigger background fetch job |
| `POST` | `/api/jobs/push` | Trigger background push job |
| `POST` | `/api/jobs/preview` | Generate dry-run preview |
| `POST` | `/api/jobs/run?confirm=true` | Trigger immediate pipeline run |
| `GET` | `/api/jobs/{id}` | Query job status and record |
| `GET` | `/api/logs` | Fetch system logs |

Local security: API binds to `127.0.0.1:12301` and supports optional token verification (`X-News-Agent-Token`).

---

## 7. Model Context Protocol (MCP) Integration

Stdio-based MCP server providing tools for local AI agents:
- Status & Config: `get_status`, `get_config_summary`
- Feed Management: `list_sources`, `add_rss_source`, `verify_source`, `update_source`, `remove_source`
- Preferences & Schedule: `set_preferences`, `set_delivery_schedule`, `set_delivery_limits`
- Job & Log Operations: `preview_digest`, `run_fetch`, `run_push`, `get_job`, `get_recent_logs`

High-impact operations (`remove_source`, `run_push`) require explicit `confirm=True` parameter.

---

## 8. Cross-Platform Lifecycle Management

| OS | Login Autostart Mechanism | Business Scheduling & Logs |
|---|---|---|
| macOS | User `LaunchAgent` (`~/Library/LaunchAgents/com.news-agent.service.plist`) | Internal `APScheduler` + File Logs |
| Windows | Task Scheduler Login Task (`NewsAgentService`) | Internal `APScheduler` + File Logs |
| Linux | User `systemd` service (`~/.config/systemd/user/news-agent.service`) | Internal `APScheduler` + File Logs |

The background service command `news-agent serve` handles Web/API, scheduling, and job execution across all operating systems.

---

## 9. Logging & Audit Architecture

Uses Python's standard `logging` library with one directory per calendar day.
The service keeps the most recent 30 daily directories by default and removes
older dated directories when logging starts. The retention window is configurable
through `log.retention_days`.

- `app.log`: Service lifecycle, scheduler events, configuration reloads.
- `fetch.log` / `push.log`: Task execution details.
- `web.log` / `mcp.log`: Access logs and interface errors.
- `audit.log`: Record of configuration and feed mutations.

---

## 10. Security Boundaries

- **Loopback Isolation:** Service binds exclusively to `127.0.0.1`.
- **SSRF Defense:** Blocks private IP ranges (RFC 1918), loopback, and local network redirects during feed fetching.
- **Secret Protection:** API keys, webhook URLs, and tokens are scrubbed from log files, MCP responses, and audit records.

---

## 11. Implementation Phasing

- **Phase 1: Foundation:** Unified directory resolution, file logging, `RunRecord`, `ConfigService`.
- **Phase 2: Local Control Plane:** FastAPI server (`news-agent serve`), REST API, Web UI.
- **Phase 3: Scheduling & Service Installation:** APScheduler integration, cross-platform autostart installers.
- **Phase 4: Agent Integration & Intelligence:** Stdio MCP server, RSS health monitoring, re-ranking optimization.

---

## 12. Technical Acceptance Criteria

- Multi-platform service deployment via CLI lifecycle commands; installers restart any existing service after updating its definition and dependencies.
- Zero external database dependencies; fully functional from local `127.0.0.1:12301` console.
- Safe concurrent job execution without duplicated fetches or notifications.
- Complete audit logging and schema validation across Web, API, and MCP channels.
