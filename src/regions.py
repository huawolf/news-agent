"""Signal region taxonomy and helpers.

Regions are a signal attribute, not a sector or an asset.  Only sectors whose
investment impact is geographically bounded use them; the remaining sectors
carry ``None`` so region filters never hide them.
"""
from __future__ import annotations

from typing import Iterable, List, Optional


GLOBAL = "GLOBAL"
CHINA = "CN"
JAPAN = "JP"
EUROPE = "EU"

#: Canonical order used by prompts, filters and user preferences.
SIGNAL_REGIONS = (GLOBAL, CHINA, JAPAN, EUROPE)
DEFAULT_USER_REGIONS = list(SIGNAL_REGIONS)

#: Sectors whose signals must carry a region.  The product taxonomy is fixed,
#: so this lives in code rather than adding another mutable sector column.
REGION_SENSITIVE_SECTORS = frozenset({"股票", "宏观经济"})


def requires_region(sector_names: Iterable[str]) -> bool:
    return any(str(name) in REGION_SENSITIVE_SECTORS for name in sector_names or ())


def normalize_signal_region(region: object, sector_names: Iterable[str]) -> Optional[str]:
    """Return the validated region for one signal.

    Region-agnostic sectors force ``None`` even when the model hallucinates a
    region.  Region-sensitive sectors fall back to ``GLOBAL`` so a missing tag
    stays visible instead of disappearing from every regional feed.
    """
    if not requires_region(sector_names):
        return None
    if isinstance(region, str):
        value = region.strip().upper()
        if value in SIGNAL_REGIONS:
            return value
    return GLOBAL


def normalize_user_regions(regions: object) -> List[str]:
    """Validate a user preference list and return it in canonical order."""
    if not isinstance(regions, (list, tuple, set)):
        raise ValueError("regions must be a list")
    selected = set()
    for raw in regions:
        value = str(raw or "").strip().upper()
        if value not in SIGNAL_REGIONS:
            raise ValueError("invalid region")
        selected.add(value)
    if not selected:
        raise ValueError("regions must not be empty")
    return [region for region in SIGNAL_REGIONS if region in selected]


def clean_user_regions(regions: object) -> List[str]:
    """Read a stored preference, falling back to all regions on bad data."""
    try:
        return normalize_user_regions(regions)
    except ValueError:
        return list(DEFAULT_USER_REGIONS)


def parse_region_filter(value: Optional[str]) -> Optional[List[str]]:
    """Parse a comma-separated query parameter; ``None`` means no filter."""
    if value is None or not str(value).strip():
        return None
    return normalize_user_regions(str(value).split(","))


__all__ = [
    "CHINA",
    "DEFAULT_USER_REGIONS",
    "EUROPE",
    "GLOBAL",
    "JAPAN",
    "REGION_SENSITIVE_SECTORS",
    "SIGNAL_REGIONS",
    "clean_user_regions",
    "normalize_signal_region",
    "normalize_user_regions",
    "parse_region_filter",
    "requires_region",
]
