from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from sqlalchemy import select
from src.api.dependencies import admin_identity, current_identity
from src.api.serializers import sector_item, signal_items, source_item, translation_item, watchlist_item, yield_pool_item
from src.auth import Identity
from src.config import get_content_languages, get_output_language, get_sync_config, MAX_PREFERENCE_CHARS
from src.db.models import GithubCache, Sector, Signal, UserProfile
from src.db.repositories import (
    dedup_stats,
    create_source,
    delete_yield_pools as delete_yield_pool_rows,
    delete_source,
    energy_balance,
    get_setting,
    get_user_profile,
    get_signal_by_id,
    get_user_preferences,
    github_translations_for,
    list_duplicates,
    list_energy_entries,
    list_peer_sync_states,
    list_sectors,
    list_sources,
    list_user_profiles,
    list_watchlist,
    list_yield_pools as list_yield_pool_rows,
    record_peer_sync,
    recent_signals,
    replace_watchlist,
    set_user_regions,
    set_user_signal_settings,
    signals_between,
    public_display_name,
    translations_updated_between,
)
from src.db.session import session_scope
from src.llm import normalize_assets
from src.runtime import default_config_path, write_config_value
from src.regions import parse_region_filter
from src.translation import resolve_locale
from src.watchlist_summary import watchlist_asset_summary


router = APIRouter()

#: On master the watchlist *is* the portal's public list (§4.1): one shared row
#: set, not one per user. Scoping it by ``user:<id>`` left
#: ``/api/server/signals?asset=…`` matching against a nonexistent owner and
#: therefore always returning an empty page.
WATCHLIST_OWNER = "public"
WATCHLIST_ASSET_CLASS_WHITELIST = frozenset({"stock", "crypto", "commodity"})


@router.get("/api/server/signals")
def public_signals(
    request: Request,
    group: str = "all",
    sector: str | None = None,
    q: str | None = None,
    direction: str | None = None,
    regions: str | None = None,
    asset: str | None = None,
    min_score: Annotated[float | None, Query(ge=0, le=100)] = None,
    locale: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict:
    if group not in {"all", "hot", "watchlist"}:
        raise HTTPException(status_code=400, detail="invalid group")
    # ``direction`` accepts a comma-separated list, so the portal can ask for
    # "bullish,bearish" (i.e. drop neutral) in one call.
    directions = _parse_directions(direction)
    try:
        region_filter = parse_region_filter(regions)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid region") from exc
    asset_symbol = None
    if asset:
        normalized = normalize_assets([asset], limit=1)
        if not normalized:
            raise HTTPException(status_code=400, detail="invalid asset")
        asset_symbol = normalized[0]
    config = request.app.state.config
    content_locale = resolve_locale(locale, config)
    main_locale = get_output_language(config)
    with session_scope() as session:
        enabled_sectors = list_sectors(session, enabled_only=True)
        allowed_names = {row.name for row in enabled_sectors}
        if sector is not None and sector not in allowed_names:
            raise HTTPException(status_code=400, detail="invalid sector")
        sector_filter = sorted(allowed_names) if group == "hot" and sector is None else None
        if group == "hot" and sector is None and not allowed_names:
            return {"items": [], "page": page, "page_size": page_size, "has_more": False}
        rows = recent_signals(
            session,
            hours=168,
            sector=sector,
            sectors=sector_filter,
            regions=region_filter,
            direction=directions,
            min_final_score=min_score,
            asset_symbol=asset_symbol,
            owner=WATCHLIST_OWNER if group == "watchlist" else None,
            watchlist_only=group == "watchlist",
            query=q,
            limit=page_size + 1,
            offset=(page - 1) * page_size,
            locale=content_locale,
            main_locale=main_locale,
        )
        if group == "watchlist" and not list_watchlist(session, WATCHLIST_OWNER):
            rows = []
        return {"items": signal_items(session, rows[:page_size], locale=content_locale, main_locale=main_locale), "page": page, "page_size": page_size, "has_more": len(rows) > page_size, "locale": content_locale, "available_locales": get_content_languages(config)}


@router.get("/api/server/watchlist-assets")
def public_watchlist_assets(
    request: Request,
    sector: str | None = None,
    q: str | None = None,
    direction: str | None = None,
    regions: str | None = None,
    min_score: Annotated[float | None, Query(ge=0, le=100)] = None,
    locale: str | None = None,
) -> dict:
    """Return the Following tab's watched assets with seven-day counts.

    Counts use the same filters as the signal list and are produced by one
    aggregation query, never one query per asset.
    """
    directions = _parse_directions(direction)
    try:
        region_filter = parse_region_filter(regions)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid region") from exc
    config = request.app.state.config
    content_locale = resolve_locale(locale, config)
    main_locale = get_output_language(config)
    with session_scope() as session:
        allowed_names = {row.name for row in list_sectors(session, enabled_only=True)}
        if sector is not None and sector not in allowed_names:
            raise HTTPException(status_code=400, detail="invalid sector")
        return {
            "items": watchlist_asset_summary(
                session,
                WATCHLIST_OWNER,
                sector=sector,
                direction=directions,
                min_final_score=min_score,
                regions=region_filter,
                query=q,
                locale=content_locale,
                main_locale=main_locale,
            )
        }


@router.get("/api/server/signals/{signal_id}")
def public_signal(request: Request, signal_id: str, locale: str | None = None) -> dict:
    config = request.app.state.config
    content_locale = resolve_locale(locale, config)
    main_locale = get_output_language(config)
    with session_scope() as session:
        row = get_signal_by_id(session, signal_id)
        if (
            row is None
            or row.score < 60
            or row.first_seen_at < datetime.now(timezone.utc) - timedelta(days=7)
        ):
            raise HTTPException(status_code=404, detail="signal not found")
        return signal_items(
            session,
            [row],
            detail=True,
            locale=content_locale,
            main_locale=main_locale,
        )[0]


@router.get("/api/server/github-trending")
def github_trending(request: Request, locale: str | None = None) -> dict:
    """Trending snapshot in the requested locale.

    The cache payload is authored in the main locale (the scorer rewrites the
    display name and the summary; the raw description stays as fallback).
    Completed ``github_translation`` rows overlay the display name and the
    summary for any other served locale. A repo still pending (or failed)
    keeps the main-locale text — the same fallback rule as ``public_signals``.
    """
    config = request.app.state.config
    content_locale = resolve_locale(locale, config)
    main_locale = get_output_language(config)
    with session_scope() as session:
        row = session.get(GithubCache, "latest")
        items = list(row.payload or []) if row else []
        if items and content_locale != main_locale:
            translations = github_translations_for(
                session, [item.get("url") for item in items], content_locale
            )
            items = [
                (
                    {
                        **item,
                        "name": translations[item["url"]].name or item.get("name") or item.get("full_name"),
                        "summary": translations[item["url"]].description or item.get("summary"),
                    }
                    if item.get("url") in translations
                    else item
                )
                for item in items
            ]
        return {
            "found": row is not None,
            "items": items,
            "fetched_at": row.fetched_at.isoformat() if row else None,
            "locale": content_locale,
        }


@router.get("/api/server/yields")
def public_yields() -> dict:
    """Current yield snapshot for the portal's wealth-management channel."""
    with session_scope() as session:
        rows = list_yield_pool_rows(session)
        return {
            "items": [yield_pool_item(row) for row in rows],
            "count": len(rows),
            "protocols": sorted({row.protocol for row in rows}),
            "chains": sorted({row.chain for row in rows}),
        }


@router.delete("/api/backoffice/yields")
def delete_yields(
    protocol: Annotated[str, Query(min_length=1, max_length=128)],
    chain: Annotated[str, Query(min_length=1, max_length=64)],
    _identity: Identity = Depends(admin_identity),
) -> dict:
    """Delete the selected protocol/chain slice from the current snapshot."""
    with session_scope() as session:
        deleted = delete_yield_pool_rows(session, protocol=protocol, chain=chain)
    return {"deleted": deleted, "protocol": protocol, "chain": chain}


@router.get("/api/server/sources")
def public_sources() -> dict:
    with session_scope() as session:
        return {"items": [source_item(row) for row in list_sources(session, "public", enabled_only=True)]}


@router.get("/api/server/sectors")
def public_sectors() -> dict:
    with session_scope() as session:
        return {"items": [sector_item(row) for row in list_sectors(session, enabled_only=True)]}


@router.get("/api/server/latest-digest")
def latest_digest(request: Request, locale: str | None = None) -> dict:
    config = request.app.state.config
    content_locale = resolve_locale(locale, config)
    main_locale = get_output_language(config)
    # Imported here: src.push.digest imports this module's serialisers, so a
    # top-level import would be circular.
    from src.push.digest import digest_key

    # Every locale has its own digest; the main locale also keeps the legacy
    # key for readers that predate the per-locale split.
    key = "latest_digest" if content_locale == main_locale else digest_key(content_locale)
    with session_scope() as session:
        raw = get_setting(session, key, "")
    if not raw:
        return {"found": False, "items": [], "generated_at": None, "locale": content_locale}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"found": False, "items": [], "generated_at": None, "locale": content_locale}
    return {"found": True, **payload}


@router.get("/api/sync/signals")
async def sync_signals(
    request: Request,
    identity: Identity = Depends(current_identity),
    since: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    locale: str | None = None,
    translations_since: str | None = None,
    translations_page: Annotated[int, Query(ge=1)] = 1,
) -> dict:
    """Incremental shared-pool feed for one slaver.

    Two independent cursors travel together:

    - ``since`` walks newly inserted signals (``first_seen_at``);
    - ``translations_since`` walks translations that completed later
      (``signal_translation.updated_at``).

    The second one exists because a signal inserted before ``since`` can still
    gain its target-language text afterwards. Without it a slaver would keep
    showing the main-locale fallback forever. Each cursor has its own page
    number, and the slaver only advances a cursor once every page of that
    stream has been consumed.
    """
    now = datetime.now(timezone.utc)
    config = request.app.state.config
    sync_config = get_sync_config(config)
    content_locale = resolve_locale(locale, config)
    main_locale = get_output_language(config)
    floor = now - timedelta(hours=sync_config.get("max_lookback_hours", 24))
    parsed = _parse_sync_cursor(since, floor)
    translation_floor = max(
        _parse_sync_cursor(translations_since, floor, "translations_since"), floor
    )
    with session_scope() as session:
        page_size = sync_config.get("page_size", 200)
        rows = signals_between(session, parsed, now, limit=page_size + 1, offset=(page - 1) * page_size)
        translation_rows = []
        translation_has_more = False
        if content_locale != main_locale:
            translation_rows = translations_updated_between(
                session,
                translation_floor,
                now,
                content_locale,
                limit=page_size + 1,
                offset=(translations_page - 1) * page_size,
            )
            translation_has_more = len(translation_rows) > page_size
        result = {
            "items": signal_items(
                session,
                rows[:page_size],
                detail=True,
                locale=content_locale,
                main_locale=main_locale,
            ),
            "page": page,
            "page_size": page_size,
            "has_more": len(rows) > page_size,
            "since": parsed.isoformat(),
            "until": now.isoformat(),
            "locale": content_locale,
            "translations": [
                translation_item(row) for row in translation_rows[:page_size]
            ],
            "translations_page": translations_page,
            "translations_has_more": translation_has_more,
            "translations_since": translation_floor.isoformat(),
        }
    if not result["has_more"] and not result["translations_has_more"]:
        with session_scope() as state_session:
            record_peer_sync(state_session, identity.user_id, now, success=True)
    return result


def _parse_sync_cursor(
    value: str | None,
    floor: datetime,
    field: str = "since",
) -> datetime:
    """Clamp a client cursor to the allowed lookback window."""
    if not value:
        return floor
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid {field}") from exc
    return max(parsed, floor)


@router.get("/api/me")
async def me(identity: Identity = Depends(current_identity)) -> dict:
    with session_scope() as session:
        profile = get_user_profile(session, identity.user_id)
        return {"user_id": identity.user_id, "username": identity.username, "display_name": public_display_name(identity.user_id, profile), "membership": identity.membership, "membership_plan": identity.membership_plan, "membership_expires_at": identity.membership_expires_at, "energy_quota": identity.energy_quota, "energy_cached": identity.energy_cached, "self_code": identity.self_code, "is_admin": identity.is_admin, "energy_balance": energy_balance(session, identity.user_id)}


@router.get("/api/me/preferences")
async def get_my_preferences(identity: Identity = Depends(current_identity)) -> dict:
    with session_scope() as session:
        return get_user_preferences(session, identity.user_id)


@router.put("/api/me/preferences")
async def put_my_preferences(
    payload: dict = Body(...),
    identity: Identity = Depends(current_identity),
) -> dict:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid payload")
    if "regions" not in payload and "signal_settings" not in payload:
        raise HTTPException(status_code=400, detail="preferences required")
    with session_scope() as session:
        try:
            if "regions" in payload:
                set_user_regions(session, identity.user_id, payload.get("regions"))
            if "signal_settings" in payload:
                set_user_signal_settings(session, identity.user_id, payload.get("signal_settings"))
            preferences = get_user_preferences(session, identity.user_id)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="invalid preferences") from exc
    return preferences


@router.get("/api/me/watchlist")
async def get_my_watchlist(identity: Identity = Depends(current_identity)) -> dict:
    with session_scope() as session:
        return {"items": [watchlist_item(row) for row in list_watchlist(session, WATCHLIST_OWNER)]}


@router.put("/api/me/watchlist")
async def put_my_watchlist(payload: dict = Body(...), identity: Identity = Depends(current_identity)) -> dict:
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="items must be a list")
    _validate_watchlist(items)
    with session_scope() as session:
        rows = replace_watchlist(session, WATCHLIST_OWNER, items)
        return {"items": [watchlist_item(row) for row in rows]}


@router.post("/api/me/watchlist")
async def add_my_watchlist(request: Request, payload: dict = Body(...), identity: Identity = Depends(current_identity)) -> dict:
    """Recognise one asset from pasted text and add it to the shared public list."""
    from src.api.console import extract_watchlist_assets
    from src.db.repositories import upsert_watchlist_asset

    text = str(payload.get("text") or "").strip()
    if not text or len(text) > 2000:
        raise HTTPException(status_code=400, detail="invalid text")
    parsed = await extract_watchlist_assets(request, text)
    with session_scope() as session:
        current = list_watchlist(session, WATCHLIST_OWNER)
        if len({row.asset for row in current} | {item["asset"] for item in parsed}) > 100:
            raise HTTPException(status_code=400, detail="too many watchlist items")
        for item in parsed:
            upsert_watchlist_asset(
                session, WATCHLIST_OWNER, item["asset"], item["asset_class"], item["aliases"]
            )
        rows = list_watchlist(session, WATCHLIST_OWNER)
        return {"items": [watchlist_item(row) for row in rows], "assets": [item["asset"] for item in parsed]}


@router.get("/api/backoffice/users")
def users(_: Identity = Depends(admin_identity)) -> dict:
    with session_scope() as session:
        return {"items": [{"user_id": row.user_id, "username": row.username, "membership": row.membership, "is_admin": row.is_admin, "last_seen_at": row.last_seen_at.isoformat()} for row in list_user_profiles(session)]}


@router.put("/api/backoffice/users/{user_id}")
def put_user(user_id: str, payload: dict, _: Identity = Depends(admin_identity)) -> dict:
    with session_scope() as session:
        row = session.get(UserProfile, user_id)
        if row is None:
            raise HTTPException(status_code=404, detail="user not found")
        if "is_admin" in payload:
            row.is_admin = bool(payload["is_admin"])
        return {"user_id": row.user_id, "is_admin": row.is_admin}


@router.get("/api/backoffice/sectors")
def sectors(_: Identity = Depends(admin_identity)) -> dict:
    with session_scope() as session:
        return {"items": [sector_item(row) for row in list_sectors(session)]}


@router.put("/api/backoffice/sectors")
def put_sectors(payload: dict, _: Identity = Depends(admin_identity)) -> dict:
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="items must be a list")
    if len(items) > 100:
        raise HTTPException(status_code=400, detail="too many sectors")
    with session_scope() as session:
        for item in items:
            if not isinstance(item, dict) or not str(item.get("name") or "").strip() or len(str(item.get("name") or "").strip()) > 64:
                raise HTTPException(status_code=400, detail="invalid sector")
            if not isinstance(item.get("keywords", []), list) or any(not isinstance(value, str) for value in item.get("keywords", [])):
                raise HTTPException(status_code=400, detail="invalid sector keywords")
            try:
                sort_weight = int(item.get("sort_weight", 0))
                sort_order = int(item.get("sort_order", 0))
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="invalid sector ordering") from exc
            row = session.get(Sector, item.get("id")) if item.get("id") else None
            if row is None:
                row = session.scalar(select(Sector).where(Sector.name == item["name"]))
            if row is None:
                row = Sector(id=uuid4().hex, name=str(item["name"]).strip())
                session.add(row)
            row.name = str(item["name"]).strip()
            row.keywords = list(item.get("keywords") or [])
            row.sort_weight = sort_weight
            row.sort_order = sort_order
            row.enabled = bool(item.get("enabled", row.enabled))
        return {"items": [sector_item(row) for row in list_sectors(session)]}


@router.post("/api/backoffice/sectors/reclassify")
async def reclassify(request: Request, _: Identity = Depends(admin_identity), hours: Annotated[int, Query(ge=1, le=720)] = 24) -> dict:
    from src.llm import DIRECTIONS, normalize_assets, score_batch
    from src.regions import normalize_signal_region
    from src.db.repositories import enabled_sector_specs

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    with session_scope() as session:
        rows = list(session.scalars(select(Signal).where(Signal.first_seen_at >= cutoff).order_by(Signal.first_seen_at.asc())))
        if not rows:
            return {"accepted": True, "hours": hours, "reclassified": 0}
        known_sectors = enabled_sector_specs(session)
        allowed = {spec["name"] for spec in known_sectors}
        scored, errors = await score_batch(
            [{"link": row.url or "", "title": row.title, "content": row.content, "summary": row.summary, "source": row.source_name} for row in rows],
            request.app.state.config.get("llm") or {},
            known_sectors,
        )
        for row, item in zip(rows, scored, strict=True):
            row.score = float(item.get("score") or row.score)
            # Reclassification writes straight to the row, so it has to repeat the
            # §5.1 normalisation the scoring path already applies; otherwise a
            # hallucinated sector name or a Chinese asset lands in the pool.
            row.sectors = [name for name in (item.get("sectors") or []) if name in allowed]
            row.assets = normalize_assets(item.get("assets")) or normalize_assets(row.assets)
            row.direction = item.get("direction") if item.get("direction") in DIRECTIONS else "neutral"
            row.region = normalize_signal_region(item.get("region"), row.sectors)
        return {"accepted": True, "hours": hours, "reclassified": len(rows), "errors": errors}


@router.get("/api/backoffice/dedup-duplicates")
def duplicates(_: Identity = Depends(admin_identity), hours: Annotated[int, Query(ge=1, le=720)] = 168, match_level: str | None = None, signal_id: str | None = None, page: Annotated[int, Query(ge=1)] = 1) -> dict:
    with session_scope() as session:
        rows = list_duplicates(session, hours=hours, match_level=match_level, signal_id=signal_id, limit=100, offset=(page - 1) * 100)
        return {"items": [{"id": row.id, "candidate_url": row.candidate_url, "candidate_title": row.candidate_title, "matched_signal_id": row.matched_signal_id, "match_level": row.match_level, "decided_by": row.decided_by, "bm25_score": row.bm25_score, "confidence": row.confidence, "reason": row.reason, "created_at": row.created_at.isoformat()} for row in rows], "page": page}


@router.get("/api/backoffice/dedup-duplicates/stats")
def duplicate_stats(_: Identity = Depends(admin_identity), hours: Annotated[int, Query(ge=1, le=720)] = 168) -> dict:
    with session_scope() as session:
        return dedup_stats(session, hours)


@router.get("/api/backoffice/energy")
def energy(_: Identity = Depends(admin_identity), user_id: str | None = None) -> dict:
    with session_scope() as session:
        return {"balance": energy_balance(session, user_id) if user_id else None, "items": [{"user_id": row.user_id, "event_type": row.event_type, "signal_id": row.signal_id, "delta": row.delta, "balance_after": row.balance_after, "created_at": row.created_at.isoformat()} for row in list_energy_entries(session, user_id=user_id)]}


@router.get("/api/backoffice/models")
def models(request: Request, _: Identity = Depends(admin_identity)) -> dict:
    config = request.app.state.config
    llm = config.get("llm") or {}
    dedup = config.get("dedup") or {}
    return {
        "llm": {"model": llm.get("model", ""), "base_url": llm.get("base_url", ""), "api_key_configured": bool(llm.get("api_key_name"))},
        "dedup": {"method": "url_bm25", "bm25_high_threshold": dedup.get("bm25_high_threshold", 20.0)},
    }


@router.get("/api/backoffice/preferences")
def backoffice_preferences(request: Request, _: Identity = Depends(admin_identity)) -> dict:
    return {"preferences": str(request.app.state.config.get("personal_preferences") or "")}


@router.put("/api/backoffice/preferences")
def put_backoffice_preferences(request: Request, payload: dict = Body(...), _: Identity = Depends(admin_identity)) -> dict:
    """Save the instance's natural-language ranking preference.

    The text feeds the scoring prompt's ``{preferences}`` slot, so it is persisted
    to ``config.json`` and mirrored into the in-memory ``llm`` section.
    """
    text = str(payload.get("preferences") or "").strip()
    if len(text) > MAX_PREFERENCE_CHARS:
        raise HTTPException(status_code=400, detail="preferences too long")
    write_config_value("personal_preferences", text, default_config_path())
    config = request.app.state.config
    config["personal_preferences"] = text
    config.setdefault("llm", {})["personal_preferences"] = text
    return {"preferences": text}


@router.get("/api/backoffice/sync-state")
def sync_state(_: Identity = Depends(admin_identity)) -> dict:
    with session_scope() as session:
        return {"items": [{"user_id": row.user_id, "last_pulled_at": row.last_pulled_at.isoformat() if row.last_pulled_at else None, "last_success_at": row.last_success_at.isoformat() if row.last_success_at else None, "last_failure_at": row.last_failure_at.isoformat() if row.last_failure_at else None} for row in list_peer_sync_states(session)]}


@router.get("/api/backoffice/sources")
def backoffice_sources(_: Identity = Depends(admin_identity)) -> dict:
    with session_scope() as session:
        return {"items": [source_item(row) for row in list_sources(session, "public")]}


@router.put("/api/backoffice/sources/{kind}/{key}")
def put_backoffice_source(kind: str, key: str, payload: dict = Body(...), _: Identity = Depends(admin_identity)) -> dict:
    if kind == "feed":
        raise HTTPException(status_code=400, detail="Feed sources are managed by the seed catalog")
    with session_scope() as session:
        rows = list_sources(session, "public", kinds=[kind])
        row = next((item for item in rows if item.key == key), None)
        if row is None:
            row = create_source(session, "public", kind, key)
        for field in ("title", "xml_url", "enabled"):
            if field in payload:
                setattr(row, field, payload[field])
        if isinstance(payload.get("params"), dict):
            row.params = payload["params"]
        session.flush()
        return source_item(row)


@router.delete("/api/backoffice/sources/{kind}/{key}")
def remove_backoffice_source(kind: str, key: str, _: Identity = Depends(admin_identity)) -> dict:
    if kind == "feed":
        raise HTTPException(status_code=400, detail="Feed sources are managed by the seed catalog")
    with session_scope() as session:
        return {"deleted": delete_source(session, "public", kind, key)}


def _parse_directions(value: str | None) -> list[str] | None:
    """Parse the comma-separated ``direction`` filter, rejecting unknown values."""
    if not value:
        return None
    directions = [item.strip() for item in value.split(",") if item.strip()]
    if not directions or any(item not in {"bullish", "bearish", "neutral"} for item in directions):
        raise HTTPException(status_code=400, detail="invalid direction")
    return directions


def _validate_watchlist(items: list) -> None:
    if len(items) > 100:
        raise HTTPException(status_code=400, detail="too many watchlist items")
    allowed_classes = WATCHLIST_ASSET_CLASS_WHITELIST
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail="invalid watchlist item")
        asset = str(item.get("asset") or "").strip().upper()
        asset_class = str(item.get("asset_class") or "stock")
        if not asset or len(asset) > 64 or not asset.isascii() or any(char.isspace() for char in asset):
            raise HTTPException(status_code=400, detail="invalid asset")
        if asset_class not in allowed_classes or asset in seen:
            raise HTTPException(status_code=400, detail="invalid watchlist item")
        seen.add(asset)
