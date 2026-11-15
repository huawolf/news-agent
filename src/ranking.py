from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable


def calculate_final_score(
    score: float,
    *,
    assets: Iterable[str] = (),
    watchlist: Iterable[str] = (),
    first_seen_at: datetime | None = None,
    source_name: str = "",
    text: str = "",
    config: dict | None = None,
) -> float:
    ranking = dict((config or {}).get("ranking") or {})
    base = max(0.0, min(100.0, float(score or 0.0)))
    watch = {str(item).strip().upper() for item in watchlist if item}
    current = {str(item).strip().upper() for item in assets if item}
    value = base
    haystack = f"{source_name} {text}".lower()
    if any(str(item).lower() in haystack for item in ranking.get("interest_keywords", [])):
        value += float(ranking.get("interest_match_bonus", 10.0))
    source_weights = ranking.get("source_weights") or {}
    value += float(source_weights.get(source_name, ranking.get("source_weight", 0.0)))
    if any(str(item).lower() in haystack for item in ranking.get("avoid_keywords", [])):
        value -= float(ranking.get("avoid_penalty", 20.0))
    if watch and watch.intersection(current):
        value += float(ranking.get("watchlist_bonus", 15.0))
    if first_seen_at:
        seen = first_seen_at
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        age_hours = max(0.0, (datetime.now(timezone.utc) - seen).total_seconds() / 3600)
        value += float(ranking.get("recency_bonus", 10.0)) * max(0.0, 1.0 - age_hours / 24.0)
    return max(0.0, min(100.0, value))
