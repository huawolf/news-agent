# News Agent - Web UI

> Part of the [system architecture](system-architecture.md). Section numbers are preserved for code cross-references.

## 11. Web UI

### 11.1 Master portal

The anonymous read-only portal is served at `/news/`.

- Investment Signals is the default top-level channel. Its secondary tabs are Hot, Following, and GitHub Trending. Hot uses the all-signals API; GitHub reads the master cache.
- Yield hides signal search, secondary tabs, and the signal list, and shows the snapshot table instead. Returning restores the previous secondary tab. The channel is read-only and omits uploader and timestamp columns.
- The signal list initially shows `final_score > 80` with bullish or bearish direction. The display-settings dialog can change direction, region, and minimum score. Region defaults to all. Display settings are stored in `localStorage`; signed-in users also sync to `user_preference`.
- Only a region subset sends the `regions` parameter. All selected means no region condition. Region-agnostic signals are retained in every region view.
- Secondary filter tabs are underline tabs below the top-level channel. Hot shows All plus enabled sectors. Following shows All plus watched assets with seven-day counts. GitHub hides both groups. Mobile tab rows scroll horizontally without wrapping.
- Changing a secondary filter or an in-group condition keeps the previous cards visible with a translucent refreshing state until the new result arrives, preventing layout collapse. The root reserves scrollbar space to avoid horizontal shift.
- Full-text search follows the current UI locale. The portal language switch supports `zh` and `en`; requests send the selected `locale`, and untranslated rows fall back to the main language. Language is mirrored to `localStorage['news-agent-language']` and same-origin manai-pic `localStorage.language` (`en` to `en-US`, `zh` to `zh-CN`). `?lng=zh|en` is kept in the URL for sharing and deep links.
- All, Hot, and Following group and format dates by `first_seen_at` in the browser time zone. Group headings show month/day, relative day, and weekday. Signal time uses `Date.toLocaleString()`.
- Anonymous signals load at most seven days and paginate automatically with `IntersectionObserver` near the list end. There is no Load more button. Signal detail also respects the seven-day window.
- A card header is time, asset name, direction, and reporter, with no source. Manual reports show a clickable `@display_name`; system collection shows a non-clickable System label. Asset names are buttons that fill the search box and search that asset. Clearing the search restores the unfiltered state.
- Source links follow the title and render from `source_urls` as original-source links, potentially multiple. The footer shows a localized region badge only when `region` is set, followed by sector names. Assets are not repeated in the footer.
- The reporter profile opens from `#reporter=<user_id>` and provides refresh-safe navigation. It shows scored total, displayable total, high-score count, overall rate, last-10 rate, and complete public history.

### 11.2 Master backoffice

`/backoffice` is a public HTML shell. Its frontend reads the same-origin manai-pic token, calls `/api/me` with Bearer auth, and reveals the console only when `is_admin = true`. Otherwise it shows a login prompt and a login link. There is no JWT paste box on master; that recovery path exists only on slaver through `/api/auth/token`.

The console has Settings and Sources tabs. Settings contains LLM, ranking preferences, watchlist, fetch/push, Feishu, and Telegram. Sources contains separate RSS and Feed lists. RSS supports create, read, update, delete, search, and manual fetch. Feed rows show fixed page URLs and explain that only slavers collect them after RSS through the local BrowserWorker daemon. Feed rows cannot be created, edited, or deleted in the backoffice.

The console router is shared with slaver through `src/api/console.py::build_console_router(guard)`. Master injects admin identity; slaver injects the loopback guard. Master rows use `owner = public`; slaver rows use `user:<id>` or the temporary placeholder.

Secrets follow one `.env` rule: a non-empty submitted value replaces the line, an empty value preserves the existing secret, and only an explicit clear action removes it. Treating an empty field as delete would erase `.env` secrets whenever the page is saved without retyping them.

Telegram binding creates a one-time token, renders `https://t.me/<bot>?start=<token>` as an offline SVG QR code, and registers the webhook with a random secret token. The callback checks `X-Telegram-Bot-Api-Secret-Token`, validates the one-time token, writes the Chat ID, clears the token, and confirms in Telegram. The page polls until the Chat ID appears.

The fetch/push panel edits fetch interval in minutes (1-1440), IANA timezone, any number of weekday/time rows, section limits, minimum push score, and topic quota. Times are edited locally and stored as cron. Saving writes `schedule` and `delivery` atomically and rebuilds `fetch` and `push:*` jobs immediately. Invalid times, timezones, intervals, or limits return 400.

Sector management, users, dedup records, energy, and sync-state panels are not currently rendered in the web UI; their `/api/backoffice/*` APIs remain available.

### 11.3 Slaver portal and console

Slaver reuses the master portal markup and visual style against its local database. It reads up to seven days of signals, enabled sectors, the GitHub cache, and any local yield snapshot. Anonymous browsing does not require login.

Following and the local console use the instance watchlist (`owner = user:0`), independent of manai-pic login state. Startup folds legacy watchlist owners into this single list. The shared header always shows the public Telegram join link, the local backoffice link, and a login or registration entry using the local one-time-state callback. After verification, the account menu shows the verified account name, manai-pic account energy, membership, invite link, and logout.

Portal display settings always mirror to browser localStorage. A signed-in user also syncs direction, minimum score, and region through local `/api/me/preferences` into `user_preference`. Anonymous use keeps local storage only.

The local console reuses shared markup and the same console endpoints. It does not require login but rejects non-loopback clients and forwarded headers. It supports LLM settings and API key input, watchlist and RSS CRUD, Feishu and Telegram settings and tests, Telegram QR binding, fetch interval, push timezone and schedules, push limits, and natural-language scoring preferences. Feed sources are read-only.

API keys live only in the project-root `.env` and are loaded into the current process. They are never written to `config.json`. The loopback console may receive the key to display it masked and offer a reveal control; save responses and logs do not echo it.

Fetch once invokes the complete slaver orchestration pipeline and shares the scheduler's asyncio lock to prevent overlapping cycles. The console intentionally omits runtime status, manai-pic login status, and shared-signal panels.

The local service uses `http://127.0.0.1:12301` without a gateway prefix. Relative `api/...` paths must work both at the local root and under `/news/`.

---

