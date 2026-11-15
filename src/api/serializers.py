from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Sequence

from sqlalchemy.orm import Session

from src.db.models import SYSTEM_REPORTER


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def signal_item(
    row: Any,
    detail: bool = False,
    *,
    locale: Optional[str] = None,
    main_locale: str = "en",
    translation: Any = None,
    reporter_name: Optional[str] = None,
) -> dict:
    """Serialize one signal in the caller's requested locale.

    The main table always holds ``main_locale``; a target locale is served from
    the completed translation row. A missing, still-pending or failed
    translation falls back to the main-table text and says so with
    ``fallback: true`` rather than hiding the signal.
    """
    title = row.title
    summary = row.summary
    keywords = list(row.keywords or [])
    fallback = False
    if locale and locale != main_locale:
        if translation is not None:
            title = translation.title or row.title
            summary = translation.summary or row.summary
            keywords = list(translation.keywords or []) or keywords
            fallback = not translation.title
        else:
            fallback = True

    reporter = row.reported_by or SYSTEM_REPORTER
    public_name = None if reporter == SYSTEM_REPORTER else (reporter_name or f"user_{reporter}")

    result = {
        "id": row.id,
        "url": row.url,
        "title": title,
        "summary": summary,
        "keywords": keywords,
        "locale": locale or main_locale,
        "fallback": fallback,
        "source_name": row.source_name,
        "published_at": iso(row.published_at),
        "first_seen_at": iso(row.first_seen_at),
        "batch_id": row.batch_id,
        "score": row.score,
        "final_score": row.final_score,
        "sectors": list(row.sectors or []),
        "assets": list(row.assets or []),
        "direction": row.direction,
        "region": row.region,
        "origin": row.origin,
        # Public name of ``signal.reported_by``: the verified manai-pic user id
        # that submitted the signal, or ``0`` when this instance collected it.
        # Legacy rows predating the default are NULL and fall back here.
        "reporter": reporter,
        "reporter_name": public_name,
        "is_github": row.is_github,
        "source_urls": list(row.source_urls or []),
    }
    if detail:
        result.update({"content": row.content})
    return result


def signal_items(
    session: Session,
    rows: Sequence[Any],
    *,
    detail: bool = False,
    locale: Optional[str] = None,
    main_locale: str = "en",
) -> list[dict]:
    """Serialize a page of signals, loading translations in one query.

    Only completed translations are eligible, so a pending row never blanks an
    otherwise readable main-table title.
    """
    translations: Dict[str, Any] = {}
    profiles: Dict[str, Any] = {}
    if locale and locale != main_locale and rows:
        from src.db.repositories import translations_for

        translations = translations_for(session, [row.id for row in rows], locale)
    if rows:
        from src.db.repositories import profiles_for, public_display_name

        reporter_ids = [row.reported_by or SYSTEM_REPORTER for row in rows]
        profiles = profiles_for(session, reporter_ids)
    return [
        signal_item(
            row,
            detail,
            locale=locale,
            main_locale=main_locale,
            translation=translations.get(row.id),
            reporter_name=(
                None
                if (row.reported_by or SYSTEM_REPORTER) == SYSTEM_REPORTER
                else public_display_name(
                    str(row.reported_by), profiles.get(str(row.reported_by))
                )
            ),
        )
        for row in rows
    ]


def translation_item(row: Any) -> dict:
    """One late-translation update for the slaver's incremental feed.

    URL-less signals use the stable master signal ID as their sync identity.
    """
    return {
        "url": (row.signal.url or "") if row.signal else "",
        "signal_id": row.signal.id if row.signal else "",
        "locale": row.locale,
        "title": row.title,
        "summary": row.summary,
        "keywords": list(row.keywords or []),
        "updated_at": iso(row.updated_at),
    }


def source_item(row: Any) -> dict:
    return {
        "kind": row.kind,
        "key": row.key,
        "title": row.title,
        "xml_url": row.xml_url,
        "enabled": row.enabled,
        "params": dict(row.params or {}),
    }


def feed_source_item(row: Any) -> dict:
    return {
        "kind": row.kind,
        "key": row.key,
        "title": row.title,
        "url": str((row.params or {}).get("url") or ""),
        "enabled": row.enabled,
    }


def watchlist_item(row: Any) -> dict:
    return {
        "asset": row.asset,
        "asset_class": row.asset_class,
        "aliases": list(row.aliases or []),
    }


def sector_item(row: Any) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "keywords": list(row.keywords or []),
        "sort_weight": row.sort_weight,
        "enabled": row.enabled,
        "sort_order": row.sort_order,
    }


def yield_pool_item(row: Any) -> dict:
    return {
        "id": row.id,
        "pool_name": row.pool_name,
        "url": row.url,
        "reference": row.reference,
        "protocol": row.protocol,
        "chain": row.chain,
        "tvl_usd": row.tvl_usd,
        "apy_30d": row.apy_30d,
        "apy_today": row.apy_today,
        "expiry_date": row.expiry_date.isoformat() if row.expiry_date else None,
    }
