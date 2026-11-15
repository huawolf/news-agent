"""Built-in adapter dispatcher (§4.1).

Every adapter is one ``fetch_source`` row whose ``kind`` selects the collector and
whose ``params`` JSON carries that collector's settings — the old
``config.json`` ``sections.<name>.*`` blocks are gone. ``enabled`` on the row is
the only switch.

All adapters emit the same entry shape as :func:`src.fetcher.parse_feed_entries`
so scoring, three-level dedup and persistence need no per-adapter branches.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Awaitable, Callable, Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from src.adapters.feed_browser import fetch_feed_sources
from src.adapters.github_trending import fetch_github_trending_entries
from src.adapters.google_news import fetch_google_news_entries
from src.adapters.hackernews import fetch_hackernews_entries
from src.adapters.signals import fetch_signal_entries
from src.db.models import FetchSource
from src.db.repositories import update_source_fingerprint

logger = logging.getLogger(__name__)

KIND_RSS = "rss"
KIND_GITHUB_TRENDING = "github_trending"
KIND_HACKERNEWS = "hackernews"
KIND_GOOGLE_NEWS = "google_news"
KIND_SIGNALS = "signals"
KIND_FEED = "feed"

ADAPTER_KINDS = (
    KIND_GITHUB_TRENDING,
    KIND_HACKERNEWS,
    KIND_GOOGLE_NEWS,
    KIND_SIGNALS,
)

__all__ = [
    "ADAPTER_KINDS",
    "KIND_FEED",
    "KIND_GITHUB_TRENDING",
    "KIND_GOOGLE_NEWS",
    "KIND_HACKERNEWS",
    "KIND_RSS",
    "KIND_SIGNALS",
    "MASTER_ONLY_KINDS",
    "fetch_adapter_source",
    "fetch_adapter_sources",
    "fetch_feed_sources",
    "is_adapter_kind",
    "select_adapter_sources",
]

# GitHub Trending is collected by master and cached; a slaver only pulls that
# cache in step 8 of its cycle (§5.6) and must never run this adapter itself.
MASTER_ONLY_KINDS = (KIND_GITHUB_TRENDING,)

Handler = Callable[[FetchSource, datetime, Optional[Session]], Awaitable[List[Dict]]]

_HANDLERS: Dict[str, Handler] = {
    KIND_GITHUB_TRENDING: fetch_github_trending_entries,
    KIND_HACKERNEWS: fetch_hackernews_entries,
    KIND_GOOGLE_NEWS: fetch_google_news_entries,
    KIND_SIGNALS: fetch_signal_entries,
}


def is_adapter_kind(kind: str) -> bool:
    return kind in _HANDLERS


def select_adapter_sources(
    sources: Sequence[FetchSource],
    *,
    is_master: bool = True,
) -> List[FetchSource]:
    """Rows this instance is allowed to run.

    ``enabled`` is the single per-row switch (§4.1). Feed pages are dispatched
    separately after RSS, and GitHub Trending is master-only.
    """
    rows = []
    for source in sources:
        kind = (source.kind or "").strip()
        if not source.enabled or not is_adapter_kind(kind):
            continue
        if kind == KIND_FEED or (not is_master and kind in MASTER_ONLY_KINDS):
            continue
        rows.append(source)
    return rows


async def fetch_adapter_source(
    source: FetchSource,
    cutoff_time: datetime,
    session: Optional[Session] = None,
) -> List[Dict]:
    """Run one adapter row. Raises on unknown ``kind`` or transport failure."""
    handler = _HANDLERS.get((source.kind or "").strip())
    if handler is None:
        raise ValueError(f"unsupported adapter kind: {source.kind!r}")
    return await handler(source, cutoff_time, session)


def _mark_status(session: Optional[Session], source: FetchSource, *, error: bool) -> None:
    """Record the cycle result on the row.

    Adapters have no ETag to store, but ``last_fetched_at`` / ``last_error_at``
    still drive the backoffice health column, so they are written here rather
    than only for RSS.
    """
    if session is None:
        return
    update_source_fingerprint(session, source.id, fetched=not error, error=error)


async def fetch_adapter_sources(
    sources: Sequence[FetchSource],
    cutoff_time: datetime,
    session: Optional[Session] = None,
    *,
    is_master: bool = True,
) -> Dict[str, List[Dict]]:
    """Run every eligible adapter row concurrently, keyed by ``source.id``.

    One adapter failing must not take the cycle down: the RSS sources and the
    other adapters still have entries to score. Failures are logged, stamped on
    the row, and the source is simply absent from the result.
    """
    adapters = select_adapter_sources(sources, is_master=is_master)
    if not adapters:
        return {}

    results = await asyncio.gather(
        *(fetch_adapter_source(source, cutoff_time, session) for source in adapters),
        return_exceptions=True,
    )

    collected: Dict[str, List[Dict]] = {}
    for source, result in zip(adapters, results, strict=True):
        if isinstance(result, BaseException):
            logger.warning(
                "adapter failed source=%s kind=%s error=%s",
                source.title,
                source.kind,
                result or type(result).__name__,
            )
            _mark_status(session, source, error=True)
            continue
        _mark_status(session, source, error=False)
        collected[source.id] = result
    return collected
