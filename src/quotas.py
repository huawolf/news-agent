"""Daily reporter scoring quota helpers.

The quota counts candidates that enter the LLM scoring path, not stored rows:
model-omitted duplicates still consumed a scoring slot, while pre-score URL
duplicates did not.  Keeping the date/window arithmetic here lets the MCP,
pipeline and API surfaces report the same ``date`` and ``resets_at`` values.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, Optional
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class QuotaWindow:
    usage_date: str
    timezone: str
    resets_at: datetime


def quota_window(
    timezone_name: str, now: Optional[datetime] = None
) -> QuotaWindow:
    name = (timezone_name or "UTC").strip() or "UTC"
    try:
        zone = ZoneInfo(name)
    except Exception:
        zone = ZoneInfo("UTC")
        name = "UTC"
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local = current.astimezone(zone)
    next_day: date = local.date() + timedelta(days=1)
    resets = datetime.combine(next_day, time.min, tzinfo=zone).astimezone(timezone.utc)
    return QuotaWindow(
        usage_date=local.date().isoformat(),
        timezone=name,
        resets_at=resets,
    )


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def quota_payload(limit: int, used: int, window: QuotaWindow) -> Dict:
    bounded_limit = max(0, int(limit))
    bounded_used = max(0, int(used))
    return {
        "limit": bounded_limit,
        "used": bounded_used,
        "remaining": max(0, bounded_limit - bounded_used),
        "date": window.usage_date,
        "timezone": window.timezone,
        "resets_at": iso_utc(window.resets_at),
    }

