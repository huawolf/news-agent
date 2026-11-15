# News Agent - System Architecture

> **Status:** Current implementation, checked against the repository on 2026-11-14.
>
> **Purpose:** Describe the implemented master/slaver investment-signal system. The master collects shared sources, serves the public portal and backoffice, and accepts reports from slavers. A slaver can pull the shared pool, collect private sources, and deliver local digests. Integrations that are not present in this repository are listed separately as external boundaries.
>
> There are exactly two runtime modes, `master` and `slaver`. A slaver connects to a master only when `server_url` is configured.
>
> **Reading guide:** start with the [README](../README.md) for installation and commands, and use [config.json.example](../config.json.example) as the authoritative configuration reference. This page covers scope, topology, deployment, identity, and security. Detailed contracts are split across the documents below; code and tests remain the final authority.

## Architecture documents

| Sections | Document | Contents |
|---|---|---|
| 1-4 | System architecture | Scope, runtime topology, gateway, identity, and security. |
| 5-6 | [Data model and seeds](data-model-and-seeds.md) | Tables, indexes, sectors, seed import, and bilingual storage. |
| 7-8 | [Processing and sync](processing-and-sync.md) | Fetch, scoring, ranking, deduplication, schedules, and sync. |
| 9-10 | [MCP and HTTP APIs](mcp-and-http-api.md) | Reporting tools, API contracts, and authentication boundaries. |
| 11 | [Web UI](web-ui.md) | Master portal, backoffice, and slaver console. |
| 12-13 | [Integrations and status](integrations-and-status.md) | External boundaries, technology decisions, implemented status, and limitations. |

Section numbers are preserved from the original architecture contract so existing code comments such as `§5.1` still map to the same headings.

---

## 1. Scope

### 1.1 Goals

- **Master/slaver separation:** one cloud master and many local slavers. A slaver can connect to the master through `server_url` or run fully locally.
- **Investment signals:** every signal can carry sectors, related assets, and a bullish/bearish/neutral direction in addition to relevance scores.
- **Public portal:** one page provides two top-level channels, Investment Signals and Yield. Investment Signals contains Hot, Following, and GitHub Trending views with sector, region, direction, score, and text filters; the API also supports `group=all`. Yield renders a separate opportunity table.
- **Administration:** the master exposes a backoffice shell and admin-protected APIs. The web console currently manages LLM, RSS sources, watchlists, preferences, delivery, and scheduling. Sectors, users, deduplication records, energy, and sync state are available through backend APIs but are not yet rendered as dedicated pages.
- **Cross-platform operation:** installers and service lifecycle helpers exist for macOS, Windows, and Linux. Native installation and recovery still require per-platform acceptance testing.
- **Shared data model:** both modes use the same SQLAlchemy 2.0 models. The master requires the configured PostgreSQL DSN; a slaver defaults to a local SQLite file.

### 1.2 Non-goals

- **No market-data terminal:** no quotes, candles, volume charts, or portfolio accounting. This is a news and signal aggregator, not a trading terminal.
- **No asset master data:** assets are normalized LLM output strings. There is no asset table, ticker validation, alias-maintenance UI, or instrument mapping.
- **No post-publication attribution:** the system logs delivery and reporting paths but does not backtest signals or attribute later price movement.
- **No in-place legacy migration:** old `config.json`, `mode_settings`, and file-storage data are not migrated. This iteration is a full refactor.

### 1.3 Implemented portal and access behavior

- The portal has two top-level channels, **Investment Signals** and **Yield**, rendered as compact text navigation with a thin active underline. Investment Signals is selected by default and contains the **Hot**, **Following**, and **GitHub Trending** tabs. Hot uses every enabled sector; there is no hardcoded sector allowlist.
- Yield reads the current `yield_pool` snapshot and renders Pool, Protocol, Chain, Reference, TVL, 30-day APY, Today APY, and Maturity. Rows are sorted by Today APY descending by default and can be filtered by protocol and chain. The channel is read-only, exposes neither upload credentials nor write timestamps, and offers no delete action. Public responses omit `reporter` and `recorded_at`. Returning to Investment Signals restores the last selected secondary tab.
- The Yield filter area always shows the bilingual risk notice. The English copy is:

  > This channel covers stablecoin yield. The listed protocols have no record of default to date. Deposit stablecoins such as USDT/USDC directly on the destination page, or swap into the corresponding stablecoin to earn yield. Note: Pendle V2 carries low risk when held to maturity; exiting early may otherwise result in a loss of principal.

- The portal toolbar provides sector selection, full-text search, search, and refresh. Direction, minimum score, and region are edited in a display-settings dialog; asset class is not a filter. Sector and region display names are localized through shared portal resources, while API and database values remain the canonical `sector.name` and region codes. Master and slaver use the same mapping.
- An empty watchlist must make `group=watchlist` return an empty result, never the full pool. The Following view shows every watched asset with its seven-day hit count, for example `NVDA(3)`, above the list. Counts and rows share the current sector, region, direction, minimum-score, and search filters. Clicking an asset applies an exact `asset` filter; clicking again or clearing the filter restores all followed signals.
- Anonymous feed and list APIs serve at most the latest seven days and do not accept a time-window argument; reporter history is the documented exception below. Signal lists support `group=all|hot|watchlist`, `sector`, `regions` as a comma-separated subset of `GLOBAL/CN/JP/EU`, `q`, `direction`, `min_score` applied as a strict comparison to `final_score`, exact `asset`, `locale`, and pagination.
- The Following asset summary is `GET /api/server/watchlist-assets`. It accepts the same filters and returns `asset`, `asset_class`, and `count_7d` for every watched asset, including zero-count rows. Counts are aggregated in application code from one signal query, not one query per asset. Summary responses are cached in-process for five minutes per owner, watchlist asset/class fingerprint, and full filter set. Watchlist edits invalidate the key immediately; newly arriving signals may take up to five minutes to enter the counts.
- A non-empty region filter keeps matching region-bearing signals plus every region-agnostic signal (`region IS NULL`). Search covers localized title, summary, and keywords plus source-language content and assets. Sectors and regions are not text-search fields. Search ranks exact asset matches first, title or keyword matches second, and other text matches third; ties preserve batch, score, and recency order.
- Signal lists default to `batch_id DESC, final_score DESC, first_seen_at DESC, id DESC`. With a search term, relevance tiers are applied first and the same ordering is used inside each tier.
- The portal default request uses `min_score=80` and `direction=bullish,bearish`; the threshold is strictly greater than 80 and neutral signals are not shown initially. The API itself does not force these defaults.
- Display settings are mirrored to browser `localStorage`. For a signed-in manai-pic user, direction, minimum score, and region are also stored in `user_preference`; anonymous use and failed preference requests keep the browser copy only.
- Every signal card shows the reporter. Manually reported signals show a clickable `@display_name`; locally collected signals show a non-clickable System label. The reporter route uses `#reporter=<user_id>`, survives refresh, and supports browser back and forward navigation.
- Reporter profiles expose `display_name`, scored and displayable counts, high-score counts, overall and last-10 high-score rates, and complete public history. The history does not use the portal's seven-day window but still returns only signals with `score >= 60`.
- A visible signal requires `score >= 60`. Rows below 60, plus aggregate, low-content, and low-value items, remain in `signal` as hidden rows for audit and reporter-rate denominators, but never appear in reader-facing queries. URL deduplication still considers hidden rows.
- Each reporter may send at most `report.daily_limit_per_user` candidates into LLM scoring per local calendar day in `report.quota_timezone`; the default is 100 in `Asia/Shanghai`. URL duplicates are removed before reservation. Over-limit items are rejected with `quota_exceeded: daily scoring limit` and never reach the scorer.
- Master admin authorization comes only from a verified manai-pic phone number or email matching `NEWS_AGENT_ADMINS`. The legacy `NEWS_AGENT_ADMIN_EMAIL` is also accepted. Email matching is case-insensitive, phone matching is exact, and an empty allowlist grants no admin access. The upstream `is_admin` value is ignored for authorization.
- Natural-language scoring preferences are editable and injected into the scoring prompt. Ranking weights are runtime configuration and are not editable in the backoffice.
- Membership data is read opportunistically from `auth.membership_database_url`. It does not grant access or enforce quotas.
- Sync and report endpoints have no rate limiter in this implementation. FastAPI documentation routes are enabled by default. A public master must restrict both at the reverse proxy until application-level controls exist.

### 1.4 Terminology

| Term | Meaning |
|---|---|
| `master` | Cloud instance: all-source collection, shared signal pool, portal, backoffice, and report ingestion. |
| `slaver` | Local instance: shared-pool pull, private-source collection, and personalized delivery. |
| Signal | A collected or reported item with scores, sectors, assets, direction, region, and provenance. |
| Sector | A configurable classification stored in `sector`; an LLM assigns enabled sector names. |
| Deduplication | URL matching when a URL exists, followed by application-level BM25. |
| Energy ledger | Local accounting entries for new pulls and reports; not the manai-pic account balance. |
| Reporter | The verified manai-pic user id in `signal.reported_by`; the public alias is `user_profile.display_name`. |
| Visible signal | A scored row with `score >= 60`; hidden rows remain stored but are excluded from reader paths. |
| High-score rate | Rows satisfying `signal.score >= 60 AND signal.final_score > 80`, divided by all scored rows for that reporter. Overall and last-10 rates require at least ten samples. |

---

## 2. Runtime Modes and Topology

```text
                        manai-pic (Go / Gin :5678)
                        /api/v1/user/me  /api/v1/user/login
                                  |
                    issues JWT    |    verifies identity
                  +---------------+----------------+
                  |                                |
         +--------v---------+          +-----------v------------+
         | master (cloud)   |          | connected slaver       |
         | Caddy -> :12301  |<-------->| `server_url` set       |
         | PostgreSQL       |  HTTPS   | SQLite (default)       |
         +--------+---------+          +-----------+------------+
                  |                                |
      +-----------+-----------+        +-----------+------------+
      | portal  | backoffice |        | local UI | MCP client  |
      | public  | admin JWT  |        | loopback | reports     |
      +---------+------------+        +----------+-------------+
```

The right-hand slaver in the diagram is the connected variant. A standalone slaver (`server_url` empty) has no manai-pic link and no master link.

The repository includes only a Caddy deployment example. Production routing is not asserted by this repository.

| Mode | Purpose | Database |
|---|---|---|
| `master` | Collect all shared sources, serve the public portal and backoffice, and accept slaver reports. It can also run the local scoring and push pipeline. | PostgreSQL DSN required. |
| `slaver`, `server_url` empty | Run independently: collect local sources and push local digests. No pull or report. | SQLite by default. |
| `slaver`, `server_url` set | Pull the master pool, collect local sources, avoid duplicate source work, and report local-only signals. | SQLite by default. |

`server_url` is a configuration value, not a mode. There is no third mode for a connected slaver.

### 2.1 Standalone slaver

A slaver with an empty `server_url` is a complete single-machine deployment. It does not require manai-pic, a master, or PostgreSQL, and it does not mount the master MCP receiver. On first start it runs Alembic against the local SQLite database, imports the bundled seeds, then serves the loopback portal and console and schedules local fetch/push jobs.

The standalone mode still needs the resources required by the work it performs:

- Network access to the enabled RSS, Feed, and adapter sources.
- An OpenAI- or Anthropic-compatible LLM endpoint for scoring and watchlist parsing.
- Delivery credentials only when a push destination is enabled.
- BrowserWorker only when Feed-page collection is enabled.

`auth.main_url`, `NEWS_AGENT_ADMINS`, and the membership database are not part of startup, collection, or local delivery in this mode. Setting `server_url` is the single switch that adds master pull/report behavior and the related manai-pic login requirement.

---

## 3. Gateway and Deployment Shape

The checked-in Caddy example mounts the service under `/news` on `vaneworker.com` and deliberately adds no `manai.cc` route. `www.vaneworker.com` redirects to the canonical apex so host-only login cookies and `localStorage` stay on one origin.

```caddyfile
www.vaneworker.com {
    redir https://vaneworker.com{uri} 308
}

vaneworker.com {
    redir /news /news/ 308
    handle_path /news/* {
        reverse_proxy 127.0.0.1:12301 {
            header_up X-Forwarded-Prefix /news
        }
    }
}
```

Use `handle_path`, not `handle` or a bare `reverse_proxy`: the application expects the prefix to be stripped and routes to remain `/` and `/api/*`. A wrong directive produces 404s because the backend receives `/news/api/...`.

| External path | Path after prefix stripping | Purpose |
|---|---|---|
| `/news` | - | 308 redirect to `/news/`. |
| `/news/` | `/` | Portal HTML. |
| `/news/api/server/signals` | `/api/server/signals` | Anonymous signal browsing. |
| `/news/api/server/yields` | `/api/server/yields` | Anonymous yield snapshot. |
| `/news/api/sync/signals` | `/api/sync/signals` | Time-window sync with JWT and a 24-hour cap. |
| `/news/api/mcp` | `/api/mcp` | MCP Streamable HTTP reporting. |
| `/news/api/backoffice/*` | `/api/backoffice/*` | Admin JWT APIs. |
| `/news/api/console/*` | `/api/console/*` | Master admin or slaver loopback console. |

Rules:

1. All frontend requests and static asset references use relative paths such as `api/...`. Absolute paths bypass the `/news` mount and can fall through to the parent site's HTML fallback, returning HTML instead of JSON.
2. The application does not embed `/news` in FastAPI routes. It reads `X-Forwarded-Prefix` only to construct the login return path.
3. There is one prefix. Do not add a second prefix such as `/investor-api`. A slaver needs only `server_url = https://vaneworker.com/news`; sync and MCP paths are joined beneath it.
4. Master login uses same-origin `/login`; slaver login uses a local one-time-state callback.

---

## 4. Identity, Authentication, and Security

### 4.1 Master

- Portal pages and `/api/server/*` are anonymous and render only public data.
- `/api/sync/*`, `/api/mcp`, `/api/me*`, `/api/console/*`, `/api/sources*`, and `/api/backoffice/*` verify `Authorization: Bearer <manai-pic JWT>` through `GET {auth.main_url}/api/v1/user/me`.
- `/backoffice` is a public HTML shell. Its JavaScript calls `/api/me` and reveals the console only when the verified admin allowlist matches.
- Admin APIs do not trust a locally stored role. A verified phone or email must match `NEWS_AGENT_ADMINS` or the legacy `NEWS_AGENT_ADMIN_EMAIL`.
- MCP validates the token during the handshake before dispatching a tool. Missing or invalid credentials fail with HTTP 401 and a JSON body, not a default identity.
- The yield snapshot path is separate. It uses a shared `REPORTER_KEY` and does not create or grant a user identity.

### 4.2 Slaver

A slaver with `server_url` configured needs a verified manai-pic identity before scheduled pull, collection, or report work. A standalone slaver has no login requirement; see [Standalone slaver](#21-standalone-slaver).

The local login flow is:

1. The UI opens the manai-pic login page.
2. manai-pic redirects back with a token.
3. The slaver verifies and stores the token locally.
4. Pull and report requests send `Authorization: Bearer <token>`.

The local token endpoint also accepts a manually supplied token for recovery.

### 4.3 Security baseline

- Identity used for pull and report accounting comes only from a validated JWT, never from the request body.
- Missing or rejected JWTs fail closed. There is no default identity.
- Anonymous reporter responses never include email, phone, or other account identifiers.
- Yield uploads require constant-time comparison of `REPORTER_KEY`.
- Sync and MCP report endpoints currently have no application-level rate limiting.

### 4.4 Energy ledger

Energy is recorded but not enforced.

| Event | Delta |
|---|---|
| Pull a new signal from master | `-0.1` |
| Pull a signal already held locally | `0` |
| Successfully report a new signal | `+1` |

`energy_ledger` stores `user_id`, `event_type`, `signal_id`, `delta`, `balance_after`, and `created_at`. Negative balances are allowed and are unrelated to the manai-pic account balance.

The portal account-energy display comes from the verified manai-pic response (`energy_cached`), not from this ledger. Invite links use the verified `self_code` and the appropriate master or local login route.

---

