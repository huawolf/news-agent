"""Per-user display preference contracts.

The browser keeps the same two preference blocks in ``localStorage``.  Signed-in
users mirror them to ``user_preference`` so a different browser can restore the
configuration without turning browser storage into the source of truth.
"""
from __future__ import annotations

import math
from typing import Dict


VALID_DIRECTIONS = ("bullish", "bearish", "neutral")
DEFAULT_SIGNAL_DIRECTIONS = ["bullish", "bearish"]
DEFAULT_MIN_SCORE = 80


def default_signal_settings() -> Dict:
    return {"directions": list(DEFAULT_SIGNAL_DIRECTIONS), "min_score": DEFAULT_MIN_SCORE}


def normalize_signal_settings(value: object) -> Dict:
    """Validate directions and score, returning a canonical settings object."""
    if not isinstance(value, dict):
        raise ValueError("signal_settings must be an object")

    raw_directions = value.get("directions")
    if not isinstance(raw_directions, (list, tuple, set)):
        raise ValueError("directions must be a list")
    selected = set()
    for raw in raw_directions:
        direction = str(raw or "").strip().lower()
        if direction not in VALID_DIRECTIONS:
            raise ValueError("invalid direction")
        selected.add(direction)
    if not selected:
        raise ValueError("directions must not be empty")

    raw_score = value.get("min_score", DEFAULT_MIN_SCORE)
    if isinstance(raw_score, bool):
        raise ValueError("min_score must be a number")
    try:
        score = float(raw_score)
    except (TypeError, ValueError) as exc:
        raise ValueError("min_score must be a number") from exc
    if not math.isfinite(score) or score < 0 or score > 100 or score != int(score):
        raise ValueError("min_score must be an integer between 0 and 100")

    return {
        "directions": [direction for direction in VALID_DIRECTIONS if direction in selected],
        "min_score": int(score),
    }


def clean_signal_settings(value: object) -> Dict:
    """Read stored settings, falling back to defaults on malformed data."""
    try:
        return normalize_signal_settings(value)
    except ValueError:
        return default_signal_settings()


__all__ = [
    "DEFAULT_MIN_SCORE",
    "DEFAULT_SIGNAL_DIRECTIONS",
    "VALID_DIRECTIONS",
    "clean_signal_settings",
    "default_signal_settings",
    "normalize_signal_settings",
]
