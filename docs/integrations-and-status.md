# News Agent - Integrations and Status

> Part of the [system architecture](system-architecture.md). Section numbers are preserved for code cross-references.

## 12. External Integrations and Technology Decisions

### 12.1 External boundaries

- **manai-pic** issues and verifies user JWTs through `/api/v1/user/me`. It is an external identity provider used by a master and by a slaver only when `server_url` is set; a standalone slaver does not depend on it. It is not part of this repository.
- **BrowserWorker** is the local browser daemon used for Feed-page collection and the external yield crawl. Its default port is 12321; `BrowserWorker_PORT` and `BrowserWorker_LOCK_FILE` override it. RSS, RSSHub-backed signals, Hacker News, Google News, and GitHub Trending do not use the browser.
- **Yield crawl:** the external `crawl-yields` manai-worker skill opens one dedicated BrowserWorker tab, calls DefiLlama's same-origin `GET /api/public/yields/earn`, and uploads through `upsert_yield_pools`. Yield observations never enter the signal table, scoring, deduplication, sector assignment, or delivery.
- **manai-worker employees, skills, and launchpad** are not in this repository. Future integrations should call this project's local APIs or MCP tools from that repository.
- **Caddy** is an example deployment file, not a managed component. This repository does not assert production route state.

### 12.2 Technology decisions

| Decision | Choice | Rationale |
|---|---|---|
| ORM and migrations | SQLAlchemy 2.0 and Alembic | One model set for two backends; declarative migration history. |
| Database ownership | Separate configured DSN and schema | Master needs PostgreSQL; slaver defaults to SQLite. Membership lookup uses a separate DSN. |
| Dedup shape | URL plus application-level BM25 | Runs before scoring on both local collection and master reporting. |
| Dedup adjudication | Fully automatic, no review queue | URL or high-threshold BM25 decides; records go to `dedup_duplicate` and logs. |
| Event-level dedup | Inject the latest 100 high-score titles and let the LLM omit duplicates | Catches rewrites that URL and BM25 miss; omitted items count as duplicates. |
| Asset representation | LLM strings, no asset table | Avoids master-data maintenance; the cost is strict spelling rules and normalization. |
| Direction | One enum per signal | Enough for grouping and filtering without a full structured signal schema. |
| Asset spelling | Uppercase English plus deterministic normalization and non-ASCII discard | `assets` is a matching key; multiple spellings would split one entity. |
| Asset language | Always English, independent of output language | Master and slaver must match assets across locales; sectors are copied verbatim for the same reason. |
| Sector assignment | LLM returns exact enabled names; keywords are hints | Enables controlled portal groups; unknown names are filtered before storage. |
| Sector seed | Bundled `sectors.seed.json` | First startup is deterministic and reproducible; upgrades only add missing keys. |
| Energy | Recorded but not enforced | The ledger can be enabled later without a schema change. |
| Chinese tokenization | `jieba` exact mode, hard dependency | Handles mixed text; storage-time tokenization keeps queries reproducible. |
| BM25 | Application implementation shared by both modes | Avoids incompatible `ts_rank_cd` and FTS5 scores and extra database extensions. |
| Title weighting | Separate title and content search columns | Title relevance is higher; default weight is 3.0. |
| Fetch cadence | Existing APScheduler, default 60 minutes | No new scheduler; signal freshness is measured in hours. |
| Pull/report timing | Same fetch cycle: pull, collect, report | Prevents reporting signals already in the master pool. |
| Incremental pull | `sync_state.last_pulled_at` plus 24-hour cap | Avoids missing late-collected items and avoids scanning the whole pool after long offline periods. |
| Cursor advance | Only after all pages succeed | A partial failure retries the same segment without leaving a hole. |
| Source catalogue | Overlap detection only | Avoids duplicate collection and duplicate signals. |
| GitHub | Master collects and caches; slaver pulls | Avoids every slaver fetching GitHub independently. |
| Sync auth | JWT separate from anonymous portal | The time window is a subset of the full pool; anonymous access would allow bulk scraping and break energy accounting. |
| Report transport | MCP Streamable HTTP in the existing FastAPI app | Machine-to-machine schema and capability discovery with standard Bearer auth. |
| MCP session | Long-lived client context across fetch cycles | Avoids paying handshake cost per batch. |
| MCP mount | `/api` mount with `/mcp` sub-path; session manager in parent lifespan | Avoids 307 redirects and the missing-lifespan failure. |
| MCP auth | ASGI wrapper, handshake validation, no SDK OAuth verifier | Only manai-pic JWT verification is needed; fail-closed HTTP 401 is clearer than a JSON-RPC error. |
| MCP body limits | 4 MiB SDK limit; 8 KB per-content truncation on slaver | Item count alone is not enough; transport exceptions cannot be classified as business rejections. |
| Report field policy | Read-only whitelist; unknown fields ignored | One dirty item cannot fail the entire batch; server-derived fields cannot be forged. |
| Visibility | `score >= 60` visible; lower and flagged rows hidden | One table, one lifecycle, partial indexes keep public queries fast. |
| Reporter name | Stable random `display_name`, editable | Never exposes email or phone; no extra source-tracking state. |
| High-score rate | `score >= 60 AND final_score > 80` over all scored rows; recent 10 require a full sample | Matches reader perception and prevents low-quality submissions from inflating a rate. |
| Reporter history | Complete public history, outside the seven-day window | Reputation should not be truncated; hidden rows remain excluded. |
| Daily quota | 100 scored candidates per reporter per quota day, reserved atomically after URL dedup | Bounds shared LLM spend without silently truncating a request. |
| Per-item fault tolerance | One invalid item enters `rejected`; the rest persist | A dirty item must not discard 200 valid signals. |
| Disconnect policy | No pending report queue; abandon gaps older than 24 hours | Simple operation; the cost is that offline-only signals do not reach the shared pool. |
| RSS fetching | ETag and Last-Modified conditional requests | Pure bandwidth and parsing savings; fingerprints persist in `fetch_source`. |
| 304 handling | Skip the source and store no XML | The body is unchanged and parsed items already exist. |
| Conditional-request coverage | Both aiohttp and Nitter requests paths | One path alone would leave the other permanently uncached. |
| Dedup audit fields | Separate `match_level` and `decided_by` | Combining them loses which stage made the decision. |
| Gateway prefix | `/news` stripped by Caddy `handle_path` | One codebase serves both master and local slaver; one rule removes the service. |
| Frontend paths | Relative only | Absolute paths can hit the parent site's SPA fallback and return HTML instead of JSON. |
| Seed timing | Startup after Alembic, before fetch | Schema belongs to Alembic; defaults belong to seed. |
| Seed upgrades | Add missing, never overwrite or delete | Backoffice edits survive upgrades; user-added sources survive. |
| Seed scope | Both modes import the full source set | A local-only slaver still gets a complete catalogue; overlap filtering prevents most duplicate work. |
| Seed failure | Startup fails | Empty sectors or sources produce a deceptively healthy but useless service. |
| Legacy compatibility | No in-place migration | Keeping both paths doubles maintenance cost. |

---

## 13. Implemented Status and Known Limitations

This section is a static repository audit, not a claim that every online environment, third-party service, or operating system has passed acceptance. Deployment and network behavior must be verified in the target environment.

Implemented:

- `master` and `slaver` modes, Alembic schema migrations, startup bootstrap, idempotent seed import, shared SQLAlchemy models, and PostgreSQL/SQLite backend selection. Master requires a DSN; slaver defaults to SQLite.
- Anonymous portal, JWT-protected master APIs, loopback-only slaver console, time-window sync, MCP Streamable HTTP reporting, UTC storage, and browser-local time display.
- RSS, Feed-page, and built-in adapter collection through the existing fetch scheduler. Local collection pre-deduplicates before scoring and rechecks before persistence. MCP reporting translates when needed, deduplicates, scores, and rechecks.
- RSS conditional requests, Feed-page cursors, application-level BM25, and database-specific candidate recall.
- Bilingual display layers and a master translation worker. Assets and sectors remain language-independent.
- Reporter aliases, public profiles, overall and last-10 high-score rates, complete public history, the `score >= 60` visibility floor, hidden rows, and a per-reporter daily scoring quota.
- Separate yield intake and a read-only portal channel. Yield rows never mix with signals.
- Responsive portal and console layouts: single-column signal cards on phones, card-style yield rows, wrapping source links, stacked console forms, and viewport-safe reporter/settings overlays.
- Portal language bridging with same-origin manai-pic, `?lng=zh|en`, report profiles, watchlist counts, and the favicon served from `static/favicon.svg`.

Known limitations:

| Area | Limitation | Impact |
|---|---|---|
| Sync ordering | A slaver still requests the master source directory after a failed shared-pool pull. | The target is to skip the catalogue request when master is unreachable. Directory failure alone falls back to collecting all local sources. |
| Ranking | `watchlist_bonus` exists in the helper but insertion paths do not pass a watchlist. | It does not affect `final_score` today. |
| Deduplication | `dedup_duplicate` has no retention job. | The audit table grows without a configured cleanup policy. |
| Security | Sync and MCP report endpoints have no rate limiting. | A valid token can drive repeated expensive requests; use proxy rate limits until implemented. |
| Security | FastAPI `/docs`, `/redoc`, and `/openapi.json` are enabled by default. | Restrict them at the proxy on a public master. |
| Reporting | Failed reports are not queued. | Signals collected while master is unreachable remain local and are not backfilled automatically. |
| Quota | The daily limit counts scored candidates, not request rate. | Repeated cheap requests are still possible; proxy rate limiting remains necessary. |
| Rewards | Reporter points and pull-spend are not implemented. | Reputation is display-only; balances do not change for high-score reporters or pulls. |
| Packaging | `resources/`, `prompts/`, and `alembic/` are outside the Python wheel package data. | `pip install news-agent` is not a supported self-contained install yet; run from source or the installer. |
| Feed collection | Feed pages depend on the external BrowserWorker daemon. | RSS and built-in adapters continue when it is absent, but the Feed section fails for that cycle. |
| Yield collection | The `crawl-yields` skill depends on BrowserWorker and DefiLlama's page API. | A failed run logs and exits non-zero without retry loops; page API changes may require a skill update. |
| Cross-platform | Install and service lifecycle code exists for macOS, Windows, and Linux. | CI runs on Linux only; native install and recovery need per-platform acceptance runs. |
| External integration | manai-worker employees, skills, and launchpad are not in this repository. | Those integrations remain external/future work. |
| External routing | Caddy is only an example. | The repository does not assert production route state or domain behavior. |
