# Implementation Roadmap

> **Note:** This document tracks implementation priorities for the target architecture. Refer to [system-architecture.md](system-architecture.md) for architectural boundaries, data contracts, and technical specifications.

---

## Current Baseline

- **Implemented:** RSS fetching, built-in signal adapters, LLM scoring, keyword deduplication, immediate push notifications, RSS / GitHub Trending / Hacker News / Insights sections, Feishu / Discord / custom webhook integration.
- **Implemented:** Dynamic RSS feed addition/blocking in `config.json`, file persistence in `news-data/` (JSON/Markdown).
- **Archived:** Legacy Linux `systemd timer` and `journald` deployment archived; replaced by cross-platform daemon architecture.
- **Completed:** Local Web/API, stdio MCP server, natural language preference ranking, built-in APScheduler, rolling file logger, macOS/Windows service lifecycle management.

---

## Milestones

### M1: Runtime Infrastructure

- [x] Define cross-platform user data directory and path resolution.
- [x] Implement application file logging: `app`, `fetch`, `push`, `web`, `mcp`, `audit` with date-based rotation and auto-cleanup.
- [x] Retain daily application log directories for 30 days by default.
- [x] Define `RunRecord` and persist `fetch`/`push`/`preview` statuses, execution sources, stats, and error summaries.
- [x] Extract `ConfigService`: schema validation, migration logic, inter-process file locks, atomic writes, revision tracking, and auditing.
- [x] Maintain backwards compatibility for CLI `fetch` and `push` commands.

**Acceptance:** Existing CLI commands function without starting Web service; invalid or concurrent writes do not corrupt valid configuration; logs operate independently of OS journals.

---

### M2: Job Executor & Local Scheduling

- [x] Connect `fetch`, `push`, and `preview` tasks to unified `JobExecutor`.
- [x] Implement mutex locking for identical job types, returning active job ID on collision, and job status querying.
- [x] Integrate `APScheduler` supporting fetch intervals and multiple delivery cron schedules.
- [x] Default unconfigured delivery to 10:00 and 20:00 daily with 10 items per delivery while preserving explicit user schedules.
- [x] Implement dynamic scheduler reloading upon configuration changes without service interruption.
- [ ] Define and test missed job execution strategy after system sleep/restart.

**Acceptance:** Scheduled triggers, CLI calls, Web API, and MCP requests do not cause duplicate fetches or pushes when triggered concurrently.

---

### M3: Local Web Console & API

- [x] Integrate FastAPI, launch `news-agent serve` binding exclusively to `127.0.0.1:12301`.
- [x] Implement REST APIs for status, config, sources, preferences, schedules, jobs, and logs.
- [x] Implement 5 Web UI pages: Dashboard/Settings, Feed Sources, Preferences, Schedules, and Execution Logs.
- [x] Add optional local token protection without exposing sensitive secrets in responses.
- [x] Support Web UI triggers for manual fetch, preview, and push execution.
- [x] Add a contextual Feishu Webhook setup tooltip beside its settings field.
- [x] Make the latest headlines the first and default Web UI tab.
- [x] Detect and expose OpenAI Chat Completions, OpenAI Responses, and Anthropic
  Messages protocols, allow manual override, and test model connectivity from
  current settings before saving.
- [x] Automatically persist visible model and delivery connection fields after
  input settles, with serialized writes and inline save status.

**Acceptance:** Users can fully manage feeds, schedules, limits, preferences, and tasks without editing raw JSON files.

---

### M4: Model Context Protocol (MCP)

- [x] Implement stdio MCP server using the official Python MCP SDK.
- [x] Expose tools for status, sources, preferences, schedules, previews, jobs, and logs.
- [x] Return configuration revision numbers and change summaries from mutation tools.
- [x] Enforce explicit confirmation flags for destructive operations (`remove_source`, `run_push`).

**Acceptance:** AI agents can perform identical configuration and task management operations via MCP as the Web UI, producing consistent state and audit logs.

---

### M5: Personalization & Feed Health

- [x] Implement topic inclusion/exclusion, source weighting, language preferences, and diversity limits.
- [x] Calibrate LLM scoring so actionable stock-investment news, evidence-backed startup opportunities, and major AI advances have equal priority.
- [x] Limit Feishu scoring alerts to LLM connection failures and timeouts while retaining all processing errors in local logs.
- [x] Combine RSS and Hacker News into one headline list capped at 10, while keeping GitHub separately numbered and capped at 3.
- [x] Strip metadata from outgoing message bodies.
- [x] Use a 3x ranked candidate pool, fill undersized digests, and mark only links actually present in delivered content as sent.
- [x] Implement deterministic re-ranking and digest item truncation on top of base LLM scores.
- [x] Implement RSS validation, explicit private network URL protection (SSRF defense), and URL deduplication.
- [x] Normalize sources into six bilingual content categories and support filtering and URL removal from the active fetch list.
- [ ] Implement feed fetch health tracking, automatic feed pausing, and ranking rationale display in previews.

**Acceptance:** User preferences reliably influence article ranking; failing feeds do not degrade long-term fetch performance.

---

### M6: Cross-Platform Service Installation

- [x] Implement macOS user-level `LaunchAgent` installer script.
- [x] Implement Windows Task Scheduler login task installer script.
- [x] Migrate Linux deployment to user-level `systemd` service executing `news-agent serve`.
- [x] Implement unified `install`, `uninstall`, and `status` service commands.
- [ ] Perform full verification on clean macOS, Windows, and Linux environments (login autostart, system reboot, sleep recovery, logging, uninstall).

**Acceptance:** All three target platforms share identical ports, configurations, data directories, log formats, Web UI, and MCP capabilities.

---

## Future Enhancements

- [ ] Optimize digest formatting and prompts: prioritize official reference links and enhance depth of insights.
- [ ] Fetch linked web page content for richer LLM summaries.
- [x] Expand supported source adapters with Product Hunt, Reddit fallback, GitHub variants, V2EX, App Store regions, domestic RSS, RSSHub-based Jike topics, and incremental Google News topic feeds for China and the United States.
- [ ] Expand verified RSS registries and source health diagnostics.
- [ ] Add delivery integrations for Zhihu, RedNote (Xiaohongshu), or custom personal blogs.
- [ ] LLM API fallback handling, cost monitoring, and usage telemetry.
- [ ] Desktop notifications for major configuration updates or push failures.

---

## Architectural Decision Log (ADR)

| Architectural Decision | Choice | Rationale |
|---|---|---|
| Execution Model | Local Background Daemon | Centralized coordination required for Web/API/MCP interfaces, in-app scheduling, and job locking. |
| Service Binding | `127.0.0.1:12301` | Loopback access only; `123001` is outside valid TCP port range (max 65535). |
| Scheduling Strategy | In-App `APScheduler` | Instant hot-reloading on config changes with unified cross-platform behavior. |
| System Lifecycle Service | Autostart Daemon Launcher Only | Avoids maintaining separate OS schedulers (`launchd`, Task Scheduler, systemd timer) for fetch and push jobs. |
| Logging Mechanism | Application Rolling File Logging | Unified log querying via Web/MCP without reliance on system-level log utilities like `journald`. |
| Agent Interface Protocol | Python MCP stdio | Simplifies local IPC without opening extra network ports. |
| Configuration Management | Centralized `ConfigService` | Prevents race conditions and corruption from concurrent Web, MCP, and job edits. |
