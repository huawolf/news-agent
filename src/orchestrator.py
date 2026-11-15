from __future__ import annotations

import logging
import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Sequence

from sqlalchemy.orm import Session
from sqlalchemy import select

from src.auth import ENERGY_PULL_DUPLICATE, ENERGY_PULL_NEW
from src.config import get_output_language, get_sync_config, get_server_url
from src.db.models import SYSTEM_REPORTER, VISIBLE_SCORE_FLOOR, FetchSource, Signal, UserProfile
from src.db.repositories import (
    advance_sync_state,
    get_signal_by_url,
    get_sync_state,
    insert_signal,
    list_sources,
    mark_sync_failure,
    record_energy,
    set_github_cache,
)
from src.db.session import refresh_sqlite_fts
from src.dedup.tokenizer import build_keywords, build_search_text, normalize_keywords
from src.dedup.urls import canonical_url, url_hash
from src.dedup.pipeline import apply_candidate, prepare_candidate
from src.llm import normalize_assets
from src.master_client import MasterAuthRequired, MasterMCPClient, MasterReportError
from src.pipeline import FetchCycleReport, run_fetch_cycle, slaver_origin
from src.ranking import calculate_final_score
from src.sync_client import MasterAuthError, MasterSyncClient, MasterSyncError

logger = logging.getLogger(__name__)


def pull_window(session: Session, config: dict, now: datetime) -> tuple[datetime, datetime]:
    now = now.astimezone(timezone.utc)
    floor = now - timedelta(hours=get_sync_config(config)["max_lookback_hours"])
    state = get_sync_state(session)
    return max(state.last_pulled_at or floor, floor), now


def translation_pull_window(session: Session, config: dict, now: datetime) -> tuple[datetime, datetime]:
    """Window for late translations, tracked by its own cursor.

    Both cursors share the same lookback floor, so a slaver that has been
    offline for more than the window simply re-reads the most recent slice
    rather than walking an unbounded history.
    """
    now = now.astimezone(timezone.utc)
    floor = now - timedelta(hours=get_sync_config(config)["max_lookback_hours"])
    state = get_sync_state(session)
    return max(state.last_translation_pulled_at or floor, floor), now


def filter_sources(sources: Sequence[FetchSource], catalog: Sequence[dict]) -> list[FetchSource]:
    """Drop the sources master already collects for the shared pool (§5.6).

    The catalog is only used to recognise overlap — the slaver never collects a
    master source on its behalf. RSS is matched on the feed URL because that is
    what identifies the feed; built-in adapters have no URL, so they match on
    ``(kind, key)``, which is the same identity the seed uses. Matching RSS on
    URL alone left every adapter running twice: master collected Hacker News
    into the shared pool while the slaver collected the same rows locally.
    """
    public_urls = {
        str(item.get("xml_url") or "").strip().rstrip("/")
        for item in catalog
        if item.get("kind") == "rss"
    }
    public_adapters = {
        (str(item.get("kind") or "").strip(), str(item.get("key") or "").strip())
        for item in catalog
        if item.get("kind") not in {"rss", "feed"}
    }
    return [
        source
        for source in sources
        if not (
            (source.kind == "rss" and source.xml_url.strip().rstrip("/") in public_urls)
            or (
                source.kind != "feed"
                and ((source.kind or "").strip(), (source.key or "").strip()) in public_adapters
            )
        )
    ]


def apply_translation(session: Session, item: dict) -> bool:
    """Overwrite the display text of an already-pulled signal.

    A slaver stores one language, so a translation that finished after the
    signal was first pulled is applied in place instead of being discarded as a
    duplicate. Only the localized fields move: ``content`` is the source-language
    body and must survive untouched, otherwise the body would silently change
    language on every locale switch.
    """
    canonical = canonical_url(str(item.get("url") or ""))
    title = str(item.get("title") or "").strip()
    if not title:
        return False
    if canonical:
        existing = get_signal_by_url(session, canonical, url_hash(canonical))
    else:
        remote_id = str(item.get("signal_id") or "").strip()
        local_id = (
            hashlib.sha256(f"master-signal:{remote_id}".encode("utf-8")).hexdigest()
            if remote_id
            else ""
        )
        existing = session.get(Signal, local_id) if local_id else None
    if existing is None:
        # The signal itself was never pulled (outside the lookback window), so
        # there is no row to localise. Its own insert will carry the translation.
        return False
    # An empty field means "nothing to say", not "erase what we have": a
    # translation may legitimately come back with a title only.
    summary = str(item.get("summary") or "").strip() or existing.summary
    keywords = normalize_keywords(item.get("keywords")) or normalize_keywords(existing.keywords)
    existing.title = title
    existing.summary = summary
    existing.keywords = keywords
    existing.title_search = build_search_text(title, existing.assets)
    existing.summary_search = build_search_text(summary, existing.assets)
    existing.keywords_search = build_search_text(
        " ".join(keywords), existing.assets
    )
    session.flush()
    refresh_sqlite_fts(session, [existing.url_hash])
    return True


def import_signal(session: Session, item: dict, user_id: str) -> bool:
    canonical = canonical_url(str(item.get("url") or ""))
    remote_id = str(item.get("id") or "").strip()
    stable_id = (
        hashlib.sha256(f"master-signal:{remote_id}".encode("utf-8")).hexdigest()
        if not canonical and remote_id
        else None
    )
    if stable_id and session.get(Signal, stable_id) is not None:
        existing = session.get(Signal, stable_id)
        record_energy(
            session,
            user_id,
            "pull_duplicate",
            ENERGY_PULL_DUPLICATE,
            existing.id,
        )
        return False
    if canonical:
        existing = get_signal_by_url(session, canonical, url_hash(canonical))
        if existing is not None:
            record_energy(session, user_id, "pull_duplicate", ENERGY_PULL_DUPLICATE, existing.id)
            return False
    else:
        prepared = prepare_candidate({**item, "url": ""})
        if prepared is None:
            return False
        duplicate, decision = apply_candidate(session, {}, prepared)
        if duplicate:
            record_energy(
                session,
                user_id,
                "pull_duplicate",
                ENERGY_PULL_DUPLICATE,
                decision.matched_signal.id,
            )
            return False
    title = str(item.get("title") or "")
    content = str(item.get("content") or "")
    summary = str(item.get("summary") or "")
    # Reuse the §5.1 normaliser rather than an ad-hoc strip/upper: a master row
    # carrying a Chinese asset must be dropped here too, otherwise the slaver
    # stores a spelling that can never match its own watchlist.
    assets = normalize_assets(item.get("assets"))
    score = float(item.get("score") or 0)
    first_seen_at = _parse_time(item.get("first_seen_at")) or datetime.now(timezone.utc)
    batch_id = str(item.get("batch_id") or f"0000-legacy-{first_seen_at.strftime('%Y%m%d')}")
    keywords = normalize_keywords(item.get("keywords")) or build_keywords(f"{title} {content}")
    reporter_id = str(item.get("reporter") or SYSTEM_REPORTER)
    reporter_name = str(item.get("reporter_name") or "").strip()
    row = insert_signal(session, {
        "id": stable_id,
        "url": canonical or None,
        "url_hash": url_hash(canonical) if canonical else prepared["url_hash"],
        "source_urls": list(item.get("source_urls") or ([canonical] if canonical else [])),
        "title": title,
        "summary": summary,
        "content": content,
        "title_search": build_search_text(title, assets),
        "summary_search": build_search_text(summary, assets),
        "keywords_search": build_search_text(" ".join(keywords), assets),
        "content_search": build_search_text(content, assets),
        "keywords": keywords,
        "source_name": str(item.get("source_name") or ""),
        "published_at": _parse_time(item.get("published_at")),
        "first_seen_at": first_seen_at,
        "batch_id": batch_id,
        "score": float(item.get("score") or 0),
        "final_score": float(
            item.get("final_score")
            or calculate_final_score(
                score,
                assets=assets,
                first_seen_at=_parse_time(item.get("first_seen_at")),
                source_name=str(item.get("source_name") or ""),
                text=f"{title} {content}",
            )
        ),
        "sectors": list(item.get("sectors") or []),
        "assets": assets[:5],
        "direction": item.get("direction") if item.get("direction") in {"bullish", "bearish", "neutral"} else "neutral",
        "region": item.get("region"),
        "origin": str(item.get("origin") or "master"),
        # Master's provenance travels with the row: a shared-pool signal keeps
        # the user who reported it, so a slaver does not show every pulled row
        # as system-collected.
        "reported_by": reporter_id,
        "is_github": bool(item.get("is_github")),
    })
    if reporter_id != SYSTEM_REPORTER:
        profile = session.get(UserProfile, reporter_id)
        if profile is None:
            session.add(
                UserProfile(
                    user_id=reporter_id,
                    display_name=reporter_name,
                    last_seen_at=datetime.now(timezone.utc),
                )
            )
        elif reporter_name:
            profile.display_name = reporter_name
    session.flush()
    refresh_sqlite_fts(session, [row.url_hash])
    record_energy(session, user_id, "pull_new", ENERGY_PULL_NEW, row.id)
    return True


async def pull_shared_signals(session: Session, config: dict, client: MasterSyncClient, user_id: str, now: datetime) -> dict:
    since, until = pull_window(session, config, now)
    translation_since, translation_until = translation_pull_window(
        session, config, now
    )
    locale = get_output_language(config)
    page = 1
    translations_page = 1
    inserted = 0
    localized = 0
    try:
        while True:
            payload = await client.pull_signals_page(
                since,
                page,
                locale=locale,
                translations_since=translation_since,
                translations_page=translations_page,
            )
            for item in payload.get("items") or []:
                inserted += int(import_signal(session, item, user_id))
            for item in payload.get("translations") or []:
                localized += int(apply_translation(session, item))
            has_more = bool(payload.get("has_more"))
            translations_has_more = bool(payload.get("translations_has_more"))
            # The two streams advance independently: the signal feed may be
            # exhausted while translations keep paging, and bumping a cursor
            # that is already at its last page would skip a whole offset.
            if has_more:
                page += 1
            if translations_has_more:
                translations_page += 1
            if not has_more and not translations_has_more:
                break
        advance_sync_state(session, until, translation_until)
    except Exception:
        mark_sync_failure(session)
        raise
    return {
        "pages": page,
        "inserted": inserted,
        "localized": localized,
        "since": since,
        "until": until,
    }


async def run_slaver_fetch_cycle(session: Session, config: dict, *, owner: str, user_id: str, token: str, now: datetime | None = None, mcp_client: MasterMCPClient | None = None) -> FetchCycleReport:
    now = now or datetime.now(timezone.utc)
    master_url = get_server_url(config)
    if not master_url:
        return await run_fetch_cycle(session, config, owner=owner, is_master=False, now=now)
    sync_client = MasterSyncClient(config, token)
    owned_client = mcp_client is None
    report_client = mcp_client or MasterMCPClient(config, token)
    try:
        try:
            await pull_shared_signals(session, config, sync_client, user_id, now)
        except (MasterAuthError, MasterSyncError) as exc:
            logger.warning("shared signal pull failed: %s", exc)
        try:
            catalog = await sync_client.list_sources()
        except MasterSyncError as exc:
            logger.info("source catalog unavailable; fetching all local sources: %s", exc)
            catalog = []
        sources = filter_sources(list_sources(session, owner, enabled_only=True), catalog)
        report = await run_fetch_cycle(session, config, owner=owner, is_master=False, now=now, sources_override=sources)
        local_rows = list(
            session.scalars(
                select(Signal).where(
                    Signal.origin == slaver_origin(owner),
                    Signal.batch_id == report.batch_id,
                    Signal.score >= VISIBLE_SCORE_FLOOR,
                )
            )
        )
        attempts = max(1, int((config.get("report") or {}).get("retry_attempts", 3)))
        delay = max(0.0, float((config.get("report") or {}).get("retry_backoff_seconds", 1.0)))
        for attempt in range(attempts):
            try:
                await report_client.report(local_rows)
                break
            except MasterAuthRequired:
                logger.warning("local signal report requires login")
                break
            except MasterReportError as exc:
                if attempt + 1 >= attempts:
                    logger.warning("local signal report failed: %s", exc)
                    break
                await asyncio.sleep(delay * (2 ** attempt))
        try:
            github = await sync_client.github_cache()
            if github.get("found"):
                set_github_cache(session, list(github.get("items") or []))
        except MasterSyncError as exc:
            logger.info("github cache unavailable: %s", exc)
        return report
    finally:
        await sync_client.close()
        if owned_client:
            await report_client.close()


def _parse_time(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
