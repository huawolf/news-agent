# News Agent - Data Model and Seeds

> Part of the [system architecture](system-architecture.md). Section numbers are preserved for code cross-references.

## 5. Data Model

The same SQLAlchemy 2.0 models and repositories support PostgreSQL, normally used by master, and SQLite, the default for slaver. `database.url` or `NEWS_AGENT_DATABASE_URL` selects the DSN. Master fails to start without a PostgreSQL DSN.

Alembic owns schema history. Migrations live in `alembic/versions/` and run at startup before seed import. Programmatic Alembic runs must preserve existing loggers (`disable_existing_loggers=False`) so migrations do not disable fetch, adapter, or pipeline logs.

| Capability | PostgreSQL | SQLite |
|---|---|---|
| JSON columns | Native `JSONB` | `JSON` stored as text |
| Candidate recall | `pg_trgm` GIN plus `ILIKE` | FTS5 virtual table |
| BM25 ranking | Application-level implementation | Same implementation |
| Tokenization | Precomputed with jieba before storage | Same implementation |
| Row ownership | Application `owner` fields | Same |

BM25 never uses PostgreSQL `ts_rank_cd` or SQLite FTS5 `bm25()`. Those score differently and would make master and slaver thresholds disagree. `pg_trgm` is part of official PostgreSQL contrib and is available in the official image without a custom build.

### 5.1 Table catalogue

| Table | Purpose | Key fields and constraints |
|---|---|---|
| `signal` | Core signal record shared by both modes. | `id`; nullable unique `url`; `url_hash`; `source_urls`; main-locale `title`, `summary`, `keywords`; source-language `content`; search columns; `source_name`; `published_at`; `first_seen_at`; `batch_id`; `score`; `final_score`; `sectors`; `assets`; `direction`; `region`; `origin`; `reported_by`; `is_github`. |
| `signal_translation` | Localized display layer for one signal. | `id`; `signal_id` with cascade delete; `locale`; localized title, summary, keywords, and search columns; `status`; `retry_count`; `last_error`; timestamps; unique `(signal_id, locale)`. |
| `github_translation` | Localized display layer for a cached GitHub repository. | Composite key `(url, locale)`; `full_name`; localized `name` and `description`; `status`; `retry_count`; `last_error`; timestamps. No foreign key to `signal`. |
| `sync_state` | Slaver pull cursors. | Singleton row; `scope = pull`; `last_pulled_at`; `last_translation_pulled_at`; `last_success_at`; `last_failure_at`. |
| `watchlist` | Followed assets. | `id`; `owner`; `asset`; `asset_class`; `aliases`; `created_at`; unique `(owner, asset)`. Master uses `public`; slaver uses `user:0`. |
| `sector` | Configurable display grouping. | `id`; unique `name`; `keywords`; `sort_weight`; `enabled`; `sort_order`. |
| `fetch_source` | Unified source registry. | `id`; `owner`; `kind`; `key`; `title`; `xml_url`; `enabled`; `params`; `etag`; `last_modified`; `last_fetched_at`; `last_error_at`; unique `(owner, kind, key)`. |
| `yield_pool` | Latest raw yield snapshot. | `id`; `pool_name`; `url`; `reference`; `protocol`; `chain`; `tvl_usd`; `apy_30d`; `apy_today`; `expiry_date`; `reporter`; `recorded_at`; unique `(pool_name, chain, protocol)`. |
| `peer_sync_state` | Master-side observation of each slaver. | `user_id`; `last_pulled_at`; `last_success_at`; `last_failure_at`; `updated_at`. |
| `github_cache` | Latest GitHub Trending snapshot. | Singleton `id = latest`; `payload` JSON list; `fetched_at`. |
| `github_push` | Repositories that appeared in a sent digest. | `url` primary key; `full_name`; `pushed_at`. |
| `signal_push` | Signals that appeared in a sent digest. | `signal_id` primary key; `url_hash`; `title`; `section`; `locale`; `pushed_at`. No suppression window. |
| `app_state` | Instance runtime marker. | Singleton; `seed_version`, displayed in the backoffice only. |
| `dedup_duplicate` | Audit record for candidates judged duplicate. | `id`; `candidate_url`; `candidate_title`; `matched_signal_id`; `match_level`; `decided_by`; `bm25_score`; `confidence`; `reason`; `created_at`. |
| `energy_ledger` | Append-only local energy accounting. | `id`; `user_id`; `event_type`; `signal_id`; `delta`; `balance_after`; `created_at`. |
| `user_profile` | Local projection of a manai-pic user, not an account store. | `user_id` primary key; `username`; stable editable `display_name`; `membership`; `energy_cached`; `self_code`; `is_admin`; `last_seen_at`. |
| `reporter_scoring_usage` | Daily LLM scoring quota. | Composite key `(user_id, usage_date)`; `scored_count`; `updated_at`. |
| `user_preference` | User-owned display preferences. | `user_id` primary key; `regions` JSON list; `signal_settings` JSON object; `updated_at`. |
| `local_setting` | Slaver-only local runtime state. | `key` primary key; `value`; `updated_at`. Stores values such as the saved JWT, user id, and login state; never written to `config.json`. |

### 5.2 Signal indexes and visibility

`signal` has a unique nullable URL, indexes on `url_hash`, `published_at`, `score`, `final_score`, `batch_id`, `first_seen_at`, `(origin, first_seen_at)`, `(region, first_seen_at)`, and `(reported_by, first_seen_at)`, plus the portal order index `(batch_id, final_score, first_seen_at, id)` and PostgreSQL GIN indexes on `assets` and `sectors`.

The feed-order, `first_seen_at`, and `(region, first_seen_at)` indexes are partial on `score >= 60` for both PostgreSQL and SQLite. The reporter index is full because hidden rows still count toward the high-score-rate denominator. Candidate-recall indexes cover visible rows only: trigram GIN on `title_search` and `content_search` in PostgreSQL, and an FTS5 virtual table in SQLite. The unique URL index remains full-table so a hidden row still blocks the same URL from being scored again.

`source_urls` stores every known source link. Only a URL-level duplicate merges the new URL into this list. A BM25 duplicate never merges, because a probabilistic match must not permanently block a URL from a later independent insertion.

---

## 6. Sectors, Seed Import, and Bilingual Model

### 6.1 Sectors and regions

The old fixed `source_categories` values are retired. Sector assignment is now:

1. Each configurable sector has an English keyword list in `sector.keywords`.
2. The scoring prompt receives enabled sector names and their keywords as hints.
3. The LLM returns `sectors` using exact enabled names. The application filters unknown names before persistence.
4. Backoffice keyword changes affect the next scoring run without a code change or reseed.

Sector names are configurable data. This document does not hardcode the seeded names; they live in `resources/sectors.seed.json`. The default seed provides five sectors covering frontier technology, equities, crypto, commodities, and macroeconomics.

The former quantitative and AI-engineering classification is not a separate sector. GitHub scoring gives extra weight to repositories with real engineering value in niche quantitative work (trading, factor models, backtesting, high-frequency systems) and AI engineering (agent frameworks, LLM infrastructure, inference optimization); generic tools and templates score lower.

Keywords are hints, not hard rules. The model chooses sectors semantically, and the application filters hallucinated names. Over-broad words should be avoided because they appear in almost every long text and raise the misclassification rate.

Regions are produced only by region-sensitive sectors:

- The configured equities and macro sectors must return a region.
- Frontier technology, crypto, and commodities must return `region = null`.
- A signal containing any region-sensitive sector must return one region; otherwise `region` is forced to `null`.
- Allowed values are `GLOBAL`, `CN`, `JP`, and `EU`.
- Invalid or missing values on region-sensitive signals fall back to `GLOBAL`; non-sensitive signals remain `null`.
- Regions do not participate in BM25, are not written into `keywords`, and are not translated with the output language.

### 6.2 First-run seed import

The database starts empty, so startup must import defaults before collection. Seeds are shipped in `resources/` and imported after Alembic migrations and before any fetch scheduling. An import failure must fail startup; an empty sector table or empty source table produces a service that appears healthy but cannot work.

| Seed | Source file | Target |
|---|---|---|
| Default sectors and keyword hints | `resources/sectors.seed.json` | `sector` |
| Default RSS sources | `resources/rss.opml` | `fetch_source`, `kind = rss` |
| Built-in adapter rows and default parameters | `resources/adapters.seed.json` | `fetch_source`, built-in `kind` values |
| Fixed Feed-page sources | `resources/feeds.seed.json` | `fetch_source`, `kind = feed` |
| Retired RSS sources | `resources/retired_sources.seed.json` | Disable existing rows matched by `xml_url`; never insert |
| Initial sync cursor | Code constant | `sync_state` |

Current seed counts are five sectors, 415 RSS sources, four built-in adapters, five Feed-page sources, and two retired sources. The RSS count is whatever `resources/rss.opml` contains; the file remains in the existing OPML format and is expanded from `outline[@type='rss']`.

The idempotency keys are `owner + kind + key` for `fetch_source` and `name` for `sector`:

```text
key exists     -> skip the whole row; do not touch enabled, params, keywords, or title
key missing    -> insert with the seed defaults
```

There is no version-based refresh. Sources disabled or keywords edited in the backoffice must survive upgrades. Sources added to a newer seed are inserted because their keys are missing; sources removed from the seed are not deleted. `app_state.seed_version` is display-only and is not a refresh trigger.

Both modes import the full seed. Master uses `owner = public`. A slaver imports with `owner = user:<id>` after login and with the temporary `pending` owner before login. The RSS key is the URL hash and does not include the user id, so the same seed file produces consistent keys on every instance.

### 6.3 Bilingual display model

`output_language` is `zh` or `en` and defines the main `signal` table language. `content_languages` lists every locale the portal serves. The first configured locale is the main locale.

The main table stores `title`, `summary`, `keywords`, `title_search`, `summary_search`, and `keywords_search` in the main language only. There is no per-row language column. Other locales live in `signal_translation` with `locale` values from `content_languages` excluding the main language.

`content` is always the collected source language and is never translated. `content` and `content_search` stay unchanged in every locale. Only display fields are localized.

Proper nouns such as company names, product names, asset names, and model names remain in their original form during translation. The translation prompt applies only to translatable natural-language content; preserving a proper noun is not considered language mixing.

Write routing:

- A signal newly collected by master writes the main language to `signal` and creates `pending` rows for every other `content_languages` locale.
- A reported signal whose `source_language` equals the main language writes directly to `signal` and queues the other locales.
- A reported signal in another language is translated into the main language before scoring. Scoring regenerates title, summary, and keywords in the main language. The reporter's original title, summary, and `source_keywords` are written directly to that locale's `signal_translation` with `status = completed`; they are not translated back again.
- A translation failure rejects the whole item (`untranslated: item`). Partial mixed-language writes are not allowed.

Translation workers process `pending` rows in batches with bounded concurrency. Failures retry up to `translation.max_retry`, then become `failed`. Pending and failed rows do not filter a signal out of lists: reading falls back to the main table and returns `fallback: true`.

Reading and search:

- A request for the main locale reads `signal` directly.
- A request for a target locale reads the `completed` translation and falls back to `signal` with `fallback: true` when it is missing.
- Search uses localized title, summary, and keywords for the requested locale. `content_search` and `assets` participate in every locale regardless of language.
- A target locale does not fall back to main-language title or summary recall, because that would return text the reader cannot read.
- Queries use the same tokenizer as ingestion, take the first five non-stopword tokens, and require token AND across searchable columns.
- If every token is removed as a stopword, the query degrades to a substring match, never to matching every row.

BM25 deduplication always uses only `title_search + content_search`. Translation fields and summary/keyword search columns do not enter BM25.

---

