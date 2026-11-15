"""Cached watched-asset counts shared by the master and slaver portals."""

from __future__ import annotations

from collections import OrderedDict
from threading import Lock
from time import monotonic
from typing import Any, Hashable, Optional, Sequence

from sqlalchemy.orm import Session

from src.db.models import Watchlist
from src.db.repositories import list_watchlist, watchlist_asset_counts


WATCHLIST_ASSET_CACHE_TTL_SECONDS = 300
WATCHLIST_ASSET_CACHE_MAX_ENTRIES = 256

_cache: "OrderedDict[Hashable, tuple[float, list[dict[str, Any]]]]" = OrderedDict()
_cache_lock = Lock()


def _clock() -> float:
    return monotonic()


def clear_watchlist_asset_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _cache_key(
    owner: str,
    rows: Sequence[Watchlist],
    *,
    sector: Optional[str],
    direction: Optional[str | Sequence[str]],
    min_final_score: Optional[float],
    regions: Optional[Sequence[str]],
    query: Optional[str],
    locale: Optional[str],
    main_locale: str,
    hours: int,
) -> tuple[Any, ...]:
    directions = (direction,) if isinstance(direction, str) else tuple(direction or ())
    return (
        owner,
        tuple((row.asset, row.asset_class) for row in rows),
        sector or "",
        tuple(sorted(directions)),
        min_final_score,
        tuple(sorted(regions or ())),
        (query or "").strip(),
        locale or "",
        main_locale,
        hours,
    )


def _cache_get(key: Hashable) -> Optional[list[dict[str, Any]]]:
    now = _clock()
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        expires_at, items = entry
        if now >= expires_at:
            _cache.pop(key, None)
            return None
        _cache.move_to_end(key)
        return items


def _cache_set(key: Hashable, items: Sequence[dict[str, Any]]) -> None:
    expires_at = _clock() + WATCHLIST_ASSET_CACHE_TTL_SECONDS
    with _cache_lock:
        _cache[key] = (expires_at, [dict(item) for item in items])
        _cache.move_to_end(key)
        while len(_cache) > WATCHLIST_ASSET_CACHE_MAX_ENTRIES:
            _cache.popitem(last=False)


def watchlist_asset_summary(
    session: Session,
    owner: str,
    *,
    sector: Optional[str] = None,
    direction: Optional[str | Sequence[str]] = None,
    min_final_score: Optional[float] = None,
    regions: Optional[Sequence[str]] = None,
    query: Optional[str] = None,
    locale: Optional[str] = None,
    main_locale: str = "en",
    hours: int = 168,
) -> list[dict[str, Any]]:
    """Return cached per-asset counts, keyed by the current watchlist and filters."""
    rows = list_watchlist(session, owner)
    key = _cache_key(
        owner,
        rows,
        sector=sector,
        direction=direction,
        min_final_score=min_final_score,
        regions=regions,
        query=query,
        locale=locale,
        main_locale=main_locale,
        hours=hours,
    )
    cached = _cache_get(key)
    if cached is not None:
        return [dict(item) for item in cached]

    items = watchlist_asset_counts(
        session,
        owner,
        hours=hours,
        sector=sector,
        direction=direction,
        min_final_score=min_final_score,
        regions=regions,
        query=query,
        locale=locale,
        main_locale=main_locale,
        watchlist_rows=rows,
    )
    _cache_set(key, items)
    return items
