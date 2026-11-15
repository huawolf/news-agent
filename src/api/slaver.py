from __future__ import annotations

import logging
import re
import secrets
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from src.api.console import extract_watchlist_assets, require_local_console
from src.auth import project, verify_token
from src.config import get_auth_config, get_content_languages, get_output_language
from src.db.seed import run_seed

from src.api.serializers import sector_item, signal_items, watchlist_item, yield_pool_item
from src.db.bootstrap import PLACEHOLDER_OWNER, SLAVER_WATCHLIST_OWNER
from src.db.models import GithubCache
from src.db.repositories import (
    get_setting,
    get_user_preferences,
    list_sectors,
    list_watchlist,
    list_yield_pools as list_yield_pool_rows,
    recent_signals,
    reassign_source_owner,
    replace_watchlist,
    set_user_regions,
    set_user_signal_settings,
    set_setting,
    upsert_watchlist_asset,
)
from src.llm import normalize_assets
from src.membership import enrich_membership
from src.regions import parse_region_filter
from src.db.session import session_scope
from src.translation import resolve_locale
from src.watchlist_summary import watchlist_asset_summary


router = APIRouter()
logger = logging.getLogger(__name__)


def _parse_directions(value: str | None) -> list[str] | None:
    """Parse the comma-separated ``direction`` filter, rejecting unknown values."""
    if not value:
        return None
    directions = [item.strip() for item in value.split(",") if item.strip()]
    if not directions or any(item not in {"bullish", "bearish", "neutral"} for item in directions):
        raise HTTPException(status_code=400, detail="invalid direction")
    return directions


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
            owner=SLAVER_WATCHLIST_OWNER if group == "watchlist" else None,
            watchlist_only=group == "watchlist",
            query=q,
            limit=page_size + 1,
            offset=(page - 1) * page_size,
            locale=content_locale,
            main_locale=main_locale,
        )
        return {
            "items": signal_items(session, rows[:page_size], locale=content_locale, main_locale=main_locale),
            "page": page,
            "page_size": page_size,
            "has_more": len(rows) > page_size,
            "locale": content_locale,
            "available_locales": get_content_languages(config),
        }


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
    """Return local watched assets with seven-day counts for the Following tab."""
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
                SLAVER_WATCHLIST_OWNER,
                sector=sector,
                direction=directions,
                min_final_score=min_score,
                regions=region_filter,
                query=q,
                locale=content_locale,
                main_locale=main_locale,
            )
        }


@router.get("/api/server/sectors")
def public_sectors() -> dict:
    with session_scope() as session:
        return {"items": [sector_item(row) for row in list_sectors(session, enabled_only=True)]}


@router.get("/api/server/github-trending")
def github_trending() -> dict:
    with session_scope() as session:
        row = session.get(GithubCache, "latest")
        return {
            "found": row is not None,
            "items": list(row.payload or []) if row else [],
            "fetched_at": row.fetched_at.isoformat() if row else None,
        }


@router.get("/api/server/yields")
def public_yields() -> dict:
    with session_scope() as session:
        rows = list_yield_pool_rows(session)
        return {
            "items": [yield_pool_item(row) for row in rows],
            "count": len(rows),
            "protocols": sorted({row.protocol for row in rows}),
            "chains": sorted({row.chain for row in rows}),
        }


@router.get("/api/status")
def status() -> dict:
    return {"status": "running", "mode": "slaver"}


@router.get("/api/auth/status")
async def auth_status(request: Request) -> dict:
    with session_scope() as session:
        user_id = get_setting(session, "user_id", "")
        token = get_setting(session, "manai_pic_jwt", "") if user_id else ""
    if not user_id or not token:
        return {"authenticated": False, "user_id": "", "username": "", "membership": "", "energy_cached": None, "self_code": ""}
    try:
        identity = await verify_token(request.app.state.config, token)
        identity = enrich_membership(identity, request.app.state.config)
    except Exception:
        with session_scope() as session:
            set_setting(session, "manai_pic_jwt", "")
            set_setting(session, "user_id", "")
        request.app.state.config["owner"] = PLACEHOLDER_OWNER
        return {"authenticated": False, "user_id": "", "username": "", "membership": "", "energy_cached": None, "self_code": ""}
    with session_scope() as session:
        project(session, identity)
    return {
        "authenticated": True,
        "user_id": identity.user_id,
        "username": identity.username,
        "membership": identity.membership_plan or identity.membership,
        "membership_expires_at": identity.membership_expires_at,
        "energy_cached": identity.energy_cached,
        "self_code": identity.self_code,
        "invite_login_url": str(request.base_url).rstrip("/") + "/api/auth/login",
    }


@router.get("/api/me/preferences")
def get_my_preferences(request: Request) -> dict:
    require_local_console(request)
    with session_scope() as session:
        user_id = get_setting(session, "user_id", "")
        if not user_id:
            raise HTTPException(status_code=401, detail="login required")
        return get_user_preferences(session, user_id)


@router.put("/api/me/preferences")
def put_my_preferences(request: Request, payload: dict = Body(...)) -> dict:
    require_local_console(request)
    with session_scope() as session:
        user_id = get_setting(session, "user_id", "")
        if not user_id:
            raise HTTPException(status_code=401, detail="login required")
        if not isinstance(payload, dict) or (
            "regions" not in payload and "signal_settings" not in payload
        ):
            raise HTTPException(status_code=400, detail="preferences required")
        try:
            if "regions" in payload:
                set_user_regions(session, user_id, payload.get("regions"))
            if "signal_settings" in payload:
                set_user_signal_settings(session, user_id, payload.get("signal_settings"))
            preferences = get_user_preferences(session, user_id)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="invalid preferences") from exc
        return preferences


@router.get("/api/me/watchlist")
def get_watchlist(request: Request) -> dict:
    require_local_console(request)
    with session_scope() as session:
        rows = list_watchlist(session, SLAVER_WATCHLIST_OWNER)
        return {"items": [watchlist_item(row) for row in rows]}


@router.put("/api/me/watchlist")
def put_watchlist(request: Request, payload: dict = Body(...)) -> dict:
    require_local_console(request)
    items = payload.get("items")
    if not isinstance(items, list) or len(items) > 100:
        raise HTTPException(status_code=400, detail="invalid items")
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail="invalid item")
        asset = str(item.get("asset") or "").strip().upper()
        asset_class = str(item.get("asset_class") or "stock")
        if not re.fullmatch(r"[A-Z0-9.]{1,32}", asset) or asset in seen:
            raise HTTPException(status_code=400, detail="invalid or duplicate asset")
        if asset_class not in {"stock", "crypto", "commodity"}:
            raise HTTPException(status_code=400, detail="invalid asset class")
        seen.add(asset)
    with session_scope() as session:
        rows = replace_watchlist(session, SLAVER_WATCHLIST_OWNER, items)
        return {"items": [watchlist_item(row) for row in rows]}


@router.post("/api/me/watchlist")
async def add_watchlist(request: Request, payload: dict = Body(...)) -> dict:
    """Recognise all assets from pasted text and add them to the instance watchlist."""
    require_local_console(request)
    text = str(payload.get("text") or "").strip()
    if not text or len(text) > 2000:
        raise HTTPException(status_code=400, detail="invalid text")
    items = await extract_watchlist_assets(request, text)
    with session_scope() as session:
        current = list_watchlist(session, SLAVER_WATCHLIST_OWNER)
        if len({row.asset for row in current} | {item["asset"] for item in items}) > 100:
            raise HTTPException(status_code=400, detail="too many watchlist items")
        for item in items:
            upsert_watchlist_asset(
                session, SLAVER_WATCHLIST_OWNER, item["asset"], item["asset_class"], item["aliases"]
            )
        rows = list_watchlist(session, SLAVER_WATCHLIST_OWNER)
        return {"items": [watchlist_item(row) for row in rows], "assets": [item["asset"] for item in items]}


@router.get("/api/signals")
def signals(request: Request, page: int = 1, page_size: int = 50, locale: str | None = None) -> dict:
    config = request.app.state.config
    content_locale = resolve_locale(locale, config)
    main_locale = get_output_language(config)
    with session_scope() as session:
        rows = recent_signals(session, hours=24, limit=min(page_size, 100) + 1, offset=max(page - 1, 0) * min(page_size, 100))
        size = min(page_size, 100)
        return {"items": signal_items(session, rows[:size], detail=True, locale=content_locale, main_locale=main_locale), "has_more": len(rows) > size, "page": page}


@router.put("/api/auth/token")
async def save_token(request: Request, payload: dict = Body(...)) -> dict:
    token = str(payload.get("token") or "").strip()
    if not token:
        return {"saved": False}
    try:
        identity = await verify_token(request.app.state.config, token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="token rejected") from exc
    with session_scope() as session:
        project(session, identity)
        set_setting(session, "manai_pic_jwt", token)
        set_setting(session, "user_id", identity.user_id)
        moved = reassign_source_owner(session, PLACEHOLDER_OWNER, identity.owner)
        run_seed(session, owner=identity.owner)
    request.app.state.config["owner"] = identity.owner
    return {"saved": True, "user_id": identity.user_id, "owner": identity.owner, "sources_reassigned": moved}


@router.get("/api/auth/login")
def login(request: Request, ivcode: str = "") -> RedirectResponse:
    state = secrets.token_urlsafe(32)
    callback = str(request.base_url).rstrip("/") + "/api/manai/auth/callback"
    with session_scope() as session:
        set_setting(session, "login_state", state)
    main_url = get_auth_config(request.app.state.config)["main_url"]
    query = {
        "desktop": "1",
        "callback": callback,
        "state": state,
    }
    if ivcode.isalnum() and len(ivcode) <= 32:
        query["ivcode"] = ivcode
    target = f"{main_url}/login?{urlencode(query)}"
    return RedirectResponse(target, status_code=302)


@router.get("/api/manai/auth/callback")
async def login_callback(
    request: Request,
    token: str = "",
    state: str = "",
) -> HTMLResponse:
    with session_scope() as session:
        expected = get_setting(session, "login_state", "")
        set_setting(session, "login_state", "")
    if not expected or not secrets.compare_digest(expected, state):
        raise HTTPException(status_code=400, detail="invalid login state")
    if not token:
        raise HTTPException(status_code=400, detail="missing token")
    try:
        identity = await verify_token(request.app.state.config, token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="token rejected") from exc
    with session_scope() as session:
        project(session, identity)
        set_setting(session, "manai_pic_jwt", token)
        set_setting(session, "user_id", identity.user_id)
        reassign_source_owner(session, PLACEHOLDER_OWNER, identity.owner)
        run_seed(session, owner=identity.owner)
    request.app.state.config["owner"] = identity.owner
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>登录成功</title><p>登录成功，正在返回门户…</p>"
        "<script>location.replace('../../../')</script><a href='../../../'>返回门户</a>"
    )


@router.delete("/api/auth/token")
def logout(request: Request) -> dict:
    with session_scope() as session:
        set_setting(session, "manai_pic_jwt", "")
        set_setting(session, "user_id", "")
    request.app.state.config["owner"] = PLACEHOLDER_OWNER
    return {"saved": False, "logged_out": True}
