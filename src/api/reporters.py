"""Public reporter profile/history APIs.

Reporter identity is a public alias only.  These routes never expose an email,
phone number or other manai-pic account field; the profile row is a cache and
the verified token remains the sole authority for writes.
"""
from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from src.api.dependencies import current_identity
from src.api.serializers import iso, signal_items
from src.auth import Identity
from src.config import get_output_language
from src.db.models import SYSTEM_REPORTER
from src.db.repositories import (
    get_user_profile,
    list_sectors,
    public_display_name,
    reporter_signals,
    reporter_statistics,
    set_user_display_name,
)
from src.db.session import session_scope
from src.llm import normalize_assets
from src.regions import parse_region_filter
from src.translation import resolve_locale


router = APIRouter()

DISPLAY_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f@]{2,32}$")


def _directions(value: str | None) -> list[str] | None:
    if not value:
        return None
    values = [item.strip() for item in value.split(",") if item.strip()]
    if not values or any(item not in {"bullish", "bearish", "neutral"} for item in values):
        raise HTTPException(status_code=400, detail="invalid direction")
    return values


def _statistics_payload(session, user_id: str) -> dict:
    stats = reporter_statistics(session, user_id)
    return {
        **stats,
        "first_reported_at": iso(stats["first_reported_at"]),
        "last_reported_at": iso(stats["last_reported_at"]),
    }


def _reporter_or_404(session, user_id: str):
    if user_id == SYSTEM_REPORTER or not user_id.strip():
        raise HTTPException(status_code=404, detail="reporter not found")
    profile = get_user_profile(session, user_id)
    stats = reporter_statistics(session, user_id)
    if profile is None and stats["scored_total"] == 0:
        raise HTTPException(status_code=404, detail="reporter not found")
    return profile


@router.get("/api/server/reporters/{user_id}")
def reporter_profile(user_id: str) -> dict:
    with session_scope() as session:
        profile = _reporter_or_404(session, user_id)
        return {
            "user_id": user_id,
            "display_name": public_display_name(user_id, profile),
            "statistics": _statistics_payload(session, user_id),
        }


@router.get("/api/server/reporters/{user_id}/signals")
def reporter_history(
    request: Request,
    user_id: str,
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
    directions = _directions(direction)
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
        _reporter_or_404(session, user_id)
        if sector is not None:
            allowed = {row.name for row in list_sectors(session, enabled_only=True)}
            if sector not in allowed:
                raise HTTPException(status_code=400, detail="invalid sector")
        rows = reporter_signals(
            session,
            user_id,
            sector=sector,
            direction=directions,
            min_final_score=min_score,
            asset_symbol=asset_symbol,
            regions=region_filter,
            query=q,
            limit=page_size + 1,
            offset=(page - 1) * page_size,
            locale=content_locale,
            main_locale=main_locale,
        )
        return {
            "items": signal_items(
                session,
                rows[:page_size],
                locale=content_locale,
                main_locale=main_locale,
            ),
            "page": page,
            "page_size": page_size,
            "has_more": len(rows) > page_size,
            "locale": content_locale,
        }


@router.put("/api/me/reporter-profile")
def update_reporter_profile(
    payload: dict = Body(...),
    identity: Identity = Depends(current_identity),
) -> dict:
    name = str(payload.get("display_name") or "").strip() if isinstance(payload, dict) else ""
    if not DISPLAY_NAME_RE.fullmatch(name):
        raise HTTPException(status_code=400, detail="invalid display_name")
    with session_scope() as session:
        row = set_user_display_name(session, identity.user_id, name)
        return {
            "user_id": identity.user_id,
            "display_name": row.display_name,
        }

