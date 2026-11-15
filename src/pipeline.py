"""The single fetch pipeline shared by master and slaver (§5.1).

Both modes run the same cycle on the same ``schedule.fetch_interval_minutes``; only the
source range differs. The order is fixed and not interchangeable:

    RSS conditional fetch → slaver-only BrowserWorker Feed pages → built-in adapters
      → 24h freshness filter
      → quality gate
      → batch LLM scoring (sectors read from the ``sector`` table)
      → asset normalisation + sector filtering (inside the scorer)
      → dedup: URL → BM25
      → insert ``signal`` with ``first_seen_at`` set at write time
      → queue target-locale translations (master only)
      → score GitHub candidates and refresh the GitHub cache (master only)

``first_seen_at`` is generated here rather than taken from the source, because it
is the window the slaver incremental pull relies on (§5.7); reusing
``published_at`` would drop every signal published before it was collected.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence
from uuid import uuid4

from sqlalchemy.orm import Session

from src.adapters import KIND_RSS, fetch_adapter_sources, fetch_feed_sources
from src.adapters.github_trending import DEFAULT_MAX_ITEMS
from src.db.models import FetchSource, VISIBLE_SCORE_FLOOR
from src.db.repositories import (
    enabled_sector_specs,
    get_github_cache,
    insert_signal,
    list_sources,
    recent_titles_for_dedup,
    reserve_scoring_slots,
    set_github_cache,
)
from src.db.session import refresh_sqlite_fts
from src.dedup.pipeline import apply as apply_dedup
from src.dedup.pipeline import prepare_candidate
from src.dedup.urls import is_valid_url
from src.fetcher import STATUS_OK, fetch_all_sources, persist_outcomes
from src.llm import SCORED_FLAG, score_batch
from src.config import get_report_config
from src.quotas import quota_window
from src.ranking import calculate_final_score
from src.translation import (
    queue_missing_github_translations,
    queue_missing_translations,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_AGE_HOURS = 24
# Freshness is a hard 24h contract: a slaver's shared-pool window is 24h wide and
# the doc caps the pull at 24h, so keeping older entries locally has no consumer.
MAX_FRESHNESS_HOURS = 24

MIN_TITLE_CHARS = 8

#: GitHub Trending keeps a rolling cache history so the portal can group repos
#: by the day they first trended (mirroring the hot-news view, §8.1).
GITHUB_HISTORY_DAYS = 7

ORIGIN_MASTER = "master"
ORIGIN_SLAVER_PREFIX = "slaver:"
OWNER_USER_PREFIX = "user:"


def slaver_origin(owner: str) -> str:
    """``slaver:<user_id>`` — never ``slaver:user:<user_id>``.

    Owners are stored as ``user:<id>`` because they key per-user rows, but the
    origin is a flat ``slaver:<user_id>`` label (§3.1); keeping the ``user:``
    prefix would make the same value unreportable to a slaver-side check.
    """
    owner = (owner or "").strip()
    if owner.startswith(OWNER_USER_PREFIX):
        owner = owner[len(OWNER_USER_PREFIX) :]
    return f"{ORIGIN_SLAVER_PREFIX}{owner}"


@dataclass
class FetchCycleReport:
    """Counters for one fetch cycle; surfaced in the backoffice health column."""

    batch_id: str = ""
    sources_total: int = 0
    sources_failed: int = 0
    entries_fetched: int = 0
    entries_dropped: int = 0
    entries_scored: int = 0
    inserted: int = 0
    hidden: int = 0
    duplicates: int = 0
    quota_rejected: int = 0
    translations_queued: int = 0
    github_cached: int = 0
    github_translations_queued: int = 0
    errors: List[str] = field(default_factory=list)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def freshness_cutoff(config: Dict, now: datetime) -> datetime:
    """``now - 24h``, never wider (§5.1)."""
    configured = int((config.get("filter") or {}).get("max_age_hours", DEFAULT_MAX_AGE_HOURS))
    hours = max(1, min(configured, MAX_FRESHNESS_HOURS))
    return _utc(now) - timedelta(hours=hours)


def passes_quality_gate(entry: Dict, cutoff: datetime) -> bool:
    """Drop what cannot become a signal.

    Freshness: only entries that carry a publish time can be judged, and an
    entry dated before the cutoff is stale. An entry with no date at all is
    kept — the feed simply does not publish one, and dropping it would silently
    lose a whole source.

    Substance: a usable title and at least one of content / summary. URLs are
    optional; URL-less signals are deduplicated with BM25 only.
    """
    link = (entry.get("link") or "").strip()
    if link and not is_valid_url(link):
        return False

    title = (entry.get("title") or "").strip()
    if len(title) < MIN_TITLE_CHARS:
        return False

    if not (entry.get("content") or "").strip() and not (entry.get("summary") or "").strip():
        return False

    published = entry.get("published")
    if isinstance(published, datetime) and _utc(published) < cutoff:
        return False

    return True


def classify_scored_entry(entry: Dict) -> tuple[bool, float]:
    """Return ``(hidden, stored_score)`` for one merged scoring result.

    Aggregate/low-content/low-value rows are stored as zero-scored hidden rows:
    they must count against the reporter's denominator but can never appear in
    a feed.  A normally scored row below the visibility floor keeps its real
    score, because the reporter's history should show *why* it was hidden.
    """
    if (
        entry.get("is_aggregate")
        or entry.get("is_low_content")
        or entry.get("is_low_value")
    ):
        return True, 0.0
    score = float(entry.get("score") or 0.0)
    return score < VISIBLE_SCORE_FLOOR, score


def collect_entries(
    outcomes: Sequence,
    adapter_entries: Dict[str, List[Dict]],
) -> List[Dict]:
    """Flatten RSS outcomes and adapter results into one entry list."""
    entries: List[Dict] = []
    for outcome in outcomes:
        if outcome.status != STATUS_OK:
            continue
        entries.extend(outcome.entries)
    for rows in adapter_entries.values():
        entries.extend(rows)
    return entries


async def run_fetch_cycle(
    session: Session,
    config: Dict,
    *,
    owner: str,
    is_master: bool,
    now: Optional[datetime] = None,
    sources_override: Optional[Sequence[FetchSource]] = None,
) -> FetchCycleReport:
    """Fetch → score → dedup → persist for one cycle."""
    now = _utc(now or datetime.now(timezone.utc))
    report = FetchCycleReport(batch_id=f"{now.strftime('%Y%m%dT%H%M%S.%fZ')}-{uuid4().hex}")
    cutoff = freshness_cutoff(config, now)

    sources = list(sources_override) if sources_override is not None else list_sources(session, owner, enabled_only=True)
    report.sources_total = len(sources)
    if not sources:
        logger.info("fetch cycle: no enabled sources for owner=%s", owner)
        return report

    rss_sources: List[FetchSource] = [s for s in sources if (s.kind or "").strip() == KIND_RSS]
    fetch_config = dict(config.get("fetch") or {})
    outcomes = await fetch_all_sources(
        rss_sources,
        cutoff,
        max_workers=int(fetch_config.get("max_workers", 10) or 10),
        timeout=int(fetch_config.get("timeout", 10) or 10),
    )
    persist_outcomes(session, outcomes)
    report.sources_failed = sum(1 for o in outcomes if o.status == "error")

    feed_sources = [
        source
        for source in sources
        if not is_master and source.enabled and source.kind == "feed"
    ]
    feed_entries = await fetch_feed_sources(
        sources,
        cutoff,
        session,
        is_master=is_master,
    )
    report.sources_failed += sum(source.id not in feed_entries for source in feed_sources)

    adapter_entries = await fetch_adapter_sources(
        sources,
        cutoff,
        session,
        is_master=is_master,
    )

    raw_entries = collect_entries(outcomes, {**adapter_entries, **feed_entries})
    report.entries_fetched = len(raw_entries)

    entries = [e for e in raw_entries if passes_quality_gate(e, cutoff)]
    report.entries_dropped = len(raw_entries) - len(entries)
    if not entries:
        logger.info("fetch cycle: nothing survived the freshness and quality gates")
        return report

    # GitHub Trending never becomes a signal row (§5.4): no dedup, no signal
    # insert. Since 2026-10 the snapshot IS LLM-scored though — the same
    # ``score_batch`` prompt ranks the deep-dive candidates (their content is
    # README-backed) and rewrites the display name plus a 3-5 sentence summary
    # in the main locale; only the top repos by score enter the cache.
    github_entries = [e for e in entries if e.get("is_github") and e.get("github")]
    entries = [e for e in entries if not (e.get("is_github") and e.get("github"))]
    if is_master and github_entries:
        sectors = enabled_sector_specs(session)
        scored, score_errors = await score_batch(github_entries, config.get("llm") or {}, sectors)
        report.errors.extend(score_errors)
        max_items = int(
            github_entries[0].get("_github_max_items") or DEFAULT_MAX_ITEMS
        )
        snapshot = _github_snapshot(github_entries, scored, max_items)
        previous = get_github_cache(session)
        payload = _merge_github_history(
            previous.payload if previous else [], snapshot, now
        )
        set_github_cache(session, payload)
        report.github_cached = len(payload)
        report.github_translations_queued = queue_missing_github_translations(
            session, payload, config
        )

    unique_entries: List[Dict] = []
    for entry in entries:
        actual_url = entry.get("link") or ""
        candidate = {**entry, "url": actual_url}
        prepared = prepare_candidate(candidate)
        if prepared is None:
            continue
        is_duplicate, _decision = await apply_dedup(session, config, prepared)
        if is_duplicate:
            report.duplicates += 1
            continue
        score_key = actual_url or f"urn:news-agent:signal:{uuid4().hex}"
        unique_entries.append({**entry, "url": actual_url, "link": score_key})

    if not unique_entries:
        logger.info("fetch cycle: all candidates were duplicates before scoring")
        return report

    quota_user_id = (
        owner[len(OWNER_USER_PREFIX) :]
        if not is_master and owner.startswith(OWNER_USER_PREFIX)
        else ""
    )
    if quota_user_id:
        report_config = get_report_config(config)
        window = quota_window(report_config["quota_timezone"], now)
        reserved = reserve_scoring_slots(
            session,
            quota_user_id,
            window.usage_date,
            report_config["daily_limit_per_user"],
            len(unique_entries),
        )
        if reserved < len(unique_entries):
            report.quota_rejected += len(unique_entries) - reserved
            logger.info(
                "fetch cycle quota exhausted user=%s date=%s rejected=%d",
                quota_user_id,
                window.usage_date,
                len(unique_entries) - reserved,
            )
            unique_entries = unique_entries[:reserved]
        if not unique_entries:
            return report

    # Sectors come from the ``sector`` table, never from the seed file: a sector
    # disabled in the backoffice must disappear from the prompt and from the
    # allowed-name filter at the same time.
    sectors = enabled_sector_specs(session)
    if not sectors:
        raise RuntimeError("no enabled sector in the sector table; run the seed first")

    duplicate_titles = (
        recent_titles_for_dedup(session, min_final_score=80.0, limit=100)
        if unique_entries
        else []
    )
    scored, score_errors = await score_batch(
        unique_entries, config.get("llm") or {}, sectors, duplicate_titles
    )
    report.errors.extend(score_errors)
    report.entries_scored = len(scored)

    origin = ORIGIN_MASTER if is_master else slaver_origin(owner)
    inserted_ids: List[str] = []
    for entry in scored:
        # The scoring prompt removes duplicate items from the response instead
        # of returning a marker. An omitted input is therefore a model-detected
        # duplicate, not an unscored signal.
        if SCORED_FLAG in entry and not entry.get(SCORED_FLAG):
            report.duplicates += 1
            continue
        hidden, stored_score = classify_scored_entry(entry)
        # ``link`` is the scoring correlation key; ``url`` remains optional.
        candidate = {
            **entry,
            "score": stored_score,
            "url": entry.get("url") if "url" in entry else entry.get("link"),
        }
        prepared = prepare_candidate(candidate)
        if prepared is None:
            continue

        if entry.get("is_duplicate") and (
            not entry.get("duplicate_of")
            or str(entry.get("duplicate_of") or "") in {
                item["id"] for item in duplicate_titles
            }
        ):
            report.duplicates += 1
            continue

        is_duplicate, _decision = await apply_dedup(session, config, prepared)
        if is_duplicate:
            report.duplicates += 1
            continue

        row = insert_signal(
            session,
            _signal_values(prepared, origin=origin, now=now, batch_id=report.batch_id, config=config),
        )
        session.flush()
        refresh_sqlite_fts(session, [row.url_hash])
        if hidden:
            report.hidden += 1
        else:
            inserted_ids.append(row.id)
            report.inserted += 1

    # Master-local signals are already in the main locale, so every configured
    # target locale is queued. A slaver never queues: it stores one language and
    # the worker is disabled outside master mode anyway.
    if is_master and inserted_ids:
        report.translations_queued = queue_missing_translations(
            session, inserted_ids, config
        )

    logger.info(
        "fetch cycle done owner=%s fetched=%d dropped=%d scored=%d inserted=%d hidden=%d quota_rejected=%d duplicates=%d github=%d",
        owner,
        report.entries_fetched,
        report.entries_dropped,
        report.entries_scored,
        report.inserted,
        report.hidden,
        report.quota_rejected,
        report.duplicates,
        report.github_cached,
    )
    return report


def _github_snapshot(
    github_entries: List[Dict], scored: List[Dict], max_items: int
) -> List[Dict]:
    """Build the ``github_cache`` payload from the LLM-scored candidates.

    The score decides what reaches the snapshot: the ``max_items`` highest
    scoring repos win. The LLM ``title`` becomes the localized display name
    and ``summary`` the 3-5 sentence digest text, both authored in the main
    locale. When scoring produced nothing usable the snapshot falls back to
    the raw trending order without any display text.
    """
    if scored:
        ranked = sorted(
            (e for e in scored if e.get("github")),
            key=lambda e: e.get("score") or 0,
            reverse=True,
        )
        return [
            {
                **e["github"],
                "score": e.get("score") or 0,
                "summary": e.get("summary") or "",
                "name": e.get("title") or "",
            }
            for e in ranked[:max_items]
        ]
    return [e["github"] for e in github_entries][:max_items]


def _parse_payload_time(value) -> Optional[datetime]:
    if isinstance(value, str) and value.strip():
        try:
            return _utc(datetime.fromisoformat(value.strip().replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _iso_payload_time(value: datetime) -> str:
    return _utc(value).isoformat()


def _merge_github_history(
    previous: List[Dict], snapshot: List[Dict], now: datetime, days: int = GITHUB_HISTORY_DAYS
) -> List[Dict]:
    """Merge the fresh top-N snapshot into a rolling 7-day history.

    The portal renders GitHub Trending like the hot-news view: grouped by day
    with one date heading per day (§8.1), so each payload entry carries the
    date the repo first trended. A repo already in the history keeps its
    earliest ``first_seen_at`` while its score, summary and name are refreshed
    from the newest scoring pass; entries older than ``days`` are dropped.
    Legacy entries without a ``first_seen_at`` (pre-history payload) start
    their history at this refresh.
    """
    cutoff = _utc(now) - timedelta(days=days)
    merged: Dict[str, Dict] = {}
    for item in previous or []:
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        seen = _parse_payload_time(item.get("first_seen_at"))
        if seen is None:
            seen = _utc(now)
        if seen < cutoff:
            continue
        merged[url] = {**item, "first_seen_at": _iso_payload_time(seen)}
    for item in snapshot or []:
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        existing = merged.get(url)
        seen = _parse_payload_time((existing or {}).get("first_seen_at")) or _utc(now)
        merged[url] = {**item, "first_seen_at": _iso_payload_time(seen)}
    return sorted(
        merged.values(),
        key=lambda item: (item.get("first_seen_at") or "", item.get("score") or 0),
        reverse=True,
    )


def _signal_values(
    prepared: Dict, *, origin: str, now: datetime, batch_id: str, config: Dict | None = None
) -> Dict:
    published = prepared.get("published")
    score = float(prepared.get("score") or 0.0)
    assets = list(prepared.get("assets") or [])
    return {
        "url": prepared["canonical_url"] or None,
        "url_hash": prepared["url_hash"],
        "source_urls": [prepared["canonical_url"]] if prepared["canonical_url"] else [],
        "title": prepared.get("title") or "",
        "summary": prepared.get("summary") or "",
        "content": prepared.get("content") or "",
        "title_search": prepared["title_search"],
        "summary_search": prepared.get("summary_search", ""),
        "keywords_search": prepared.get("keywords_search", ""),
        "content_search": prepared["content_search"],
        "keywords": list(prepared.get("keywords") or []),
        "source_name": prepared.get("source") or "",
        "published_at": _utc(published) if isinstance(published, datetime) else None,
        # Generated here on purpose: §5.7 windows the slaver pull on this column.
        "first_seen_at": now,
        "batch_id": batch_id,
        "score": score,
        "final_score": calculate_final_score(
            score,
            assets=assets,
            first_seen_at=now,
            source_name=prepared.get("source") or "",
            text=f"{prepared.get('title') or ''} {prepared.get('content') or ''}",
            config=config,
        ),
        "sectors": list(prepared.get("sectors") or []),
        "assets": assets,
        "direction": prepared.get("direction") or "neutral",
        "region": prepared.get("region"),
        "origin": origin,
        "is_github": bool(prepared.get("is_github")),
    }
