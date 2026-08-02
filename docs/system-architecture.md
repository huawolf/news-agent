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
- Generic feed additions are limited to RSS/Atom feeds in Phase 1; GitHub Trending and Hacker News remain built-in sections.

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
- **Config Service:** Schema validation, atomic reads/writes, automatic backups, and notifying the Scheduler of changes.
- **Logging & Audit Service:** Manages rolling log files and audits configuration modifications made by users or agents.

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
  backups/
```

- `news-data/`: Retains existing raw payload formats (`fetch-*.json`, `push-*.md`, `notify-*.md`).
- `runs/`: Stores structured JSON job execution summaries and statuses.

### 3.2 Configuration Schema Evolution

Top-level configuration schema with backward compatibility:

```json
{
  "personal_preferences": "Prioritize AI agents, model releases, and practical developer tools. Prefer Chinese summaries.",
  "delivery": {
    "timezone": "Asia/Shanghai",
    "schedules": [
      {
        "id": "morning",
        "cron": "0 8 * * *",
        "max_items": 8,
        "sections": ["rss", "github", "hackernews", "insights"]
      },
      {
        "id": "evening",
        "cron": "0 17 * * *",
        "max_items": 5,
        "sections": ["rss"]
      }
    ],
    "immediate": {"enabled": true, "threshold": 92, "daily_limit": 3}
  }
}
```

Legacy `schedule.push_cron` settings are automatically migrated to `delivery.schedules` upon initial load.

### 3.3 Mutation Safeguards & Concurrency Controls

- **Validation:** Pydantic models validate types, value ranges, URLs, cron expressions, and webhook configurations.
- **Atomic File Operations:** Uses inter-process file locks, temporary swap files, and atomic replaces to prevent corruption during concurrent edits.
- **Versioning:** Increments configuration revision numbers on write and maintains historic snapshots in `backups/`.
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

### 4.2 Preference Prompting & Deterministic Re-ranking

Natural language preference string `personal_preferences` guides LLM scoring. The final ranking uses a deterministic scoring formula:

```text
final_score = base_llm_score
            + interest_match_bonus
            + source_weight
            + recency_bonus
            - avoid_penalty
            - duplicate_penalty
```

Final digests are truncated according to the `max_items` parameter of the active schedule.

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

1. **Settings:** Preferences, secret state indicators, schedule management, and test triggers.
2. **Sources:** Bulk RSS/Atom feed insertion, validation, and source management.
3. **Logs:** Real-time application log viewer (sanitized of secret keys).

### REST API Surface

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/status` | System status and service health |
| `GET` | `/api/config` | Fetch current configuration |
| `PUT` | `/api/config/preferences` | Update legacy structured preferences |
| `PUT` | `/api/personal-preferences` | Update natural language preference statement |
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

Uses Python standard `logging` library with `RotatingFileHandler`:
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

- Multi-platform service deployment via CLI installer commands.
- Zero external database dependencies; fully functional from local `127.0.0.1:12301` console.
- Safe concurrent job execution without duplicated fetches or notifications.
- Complete audit logging and schema validation across Web, API, and MCP channels.
