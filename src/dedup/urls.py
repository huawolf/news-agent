"""URL normalisation used as the first dedup level and as the storage key."""
from __future__ import annotations

import hashlib
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

TRACKING_PREFIXES = ("utm_",)
TRACKING_KEYS = {
    "spm",
    "from",
    "ref",
    "oc",
    "fbclid",
    "gclid",
    "source",
    "share_token",
    "wt_mc",
}


def canonical_url(url: str) -> str:
    """Strip tracking parameters, fragments and trailing slashes.

    Lowercases scheme and host so the same article fetched from two variants of
    a site collapses to one key.
    """
    if not url or not isinstance(url, str):
        return ""
    clean = url.strip()
    try:
        parsed = urlparse(clean)
    except Exception:
        return clean

    if not parsed.scheme or not parsed.netloc:
        return clean

    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/")

    kept = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lower = key.lower()
        if lower.startswith(TRACKING_PREFIXES) or lower in TRACKING_KEYS:
            continue
        kept.append((key, value))

    return urlunparse((scheme, netloc, path, parsed.params, urlencode(kept), ""))


def url_hash(url: str) -> str:
    """SHA-256 of the canonical URL; indexed for O(1) dedup lookups."""
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()


def is_valid_url(url: Optional[str]) -> bool:
    if not url or not isinstance(url, str) or len(url) > 2048:
        return False
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False
    return bool(parsed.scheme in ("http", "https") and parsed.netloc)