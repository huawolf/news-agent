"""Google News adapter (``kind = google_news``).

Region × topic feeds are expanded by the adapter itself, so they stay inside
``params`` instead of becoming ``fetch_source`` rows (§4.1).

The per-feed incremental cursors used to live in ``<data_dir>/google-news-state.json``.
File storage is gone; they now live in the ``local_setting`` table under
``google_news.cursors`` as one JSON blob, which keeps them transactional with
the rest of the instance state.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from urllib.parse import urlencode

import aiohttp
import feedparser
from sqlalchemy.orm import Session

from src.db.models import FetchSource
from src.db.repositories import get_setting, set_setting

logger = logging.getLogger(__name__)

CURSOR_SETTING_KEY = "google_news.cursors"

REGIONS = {
    "cn": {"label": "China", "hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"},
    "us": {"label": "United States", "hl": "en-US", "gl": "US", "ceid": "US:en"},
}

TOPICS = {
    "business": "BUSINESS",
    "technology": "TECHNOLOGY",
    "science": "SCIENCE",
    "cryptocurrency": "CRYPTOCURRENCY",
}

SEARCH_TOPICS = {
    "cryptocurrency": {
        "cn": "加密货币 OR 比特币 OR 以太坊 OR 稳定币",
        "us": "cryptocurrency OR crypto OR bitcoin OR ethereum OR stablecoin",
    },
}

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NewsAgent/0.3; +https://github.com/)",
    "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
}

DEFAULT_TIMEOUT = 10
DEFAULT_MAX_ITEMS = 20
MAX_LOOKBACK_HOURS = 24


def feed_id(region_id: str, topic_id: str) -> str:
    return f"google-news-{region_id}-{topic_id}"


def feed_url(region_id: str, topic_id: str) -> str:
    region = REGIONS[region_id]
    query = SEARCH_TOPICS.get(topic_id, {}).get(region_id)
    if query:
        params = urlencode(
            {
                "q": query,
                "hl": region["hl"],
                "gl": region["gl"],
                "ceid": region["ceid"],
            }
        )
        return f"https://news.google.com/rss/search?{params}"
    return (
        f"https://news.google.com/rss/headlines/section/topic/{TOPICS[topic_id]}"
        f"?hl={region['hl']}&gl={region['gl']}&ceid={region['ceid']}"
    )


def feed_title(region_id: str, topic_id: str) -> str:
    return f"Google News {REGIONS[region_id]['label']} {TOPICS[topic_id].title()}"


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parse_cursor(value: object) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return _utc(parsed)


def load_cursors(session: Optional[Session]) -> Dict[str, str]:
    if session is None:
        return {}
    raw = get_setting(session, CURSOR_SETTING_KEY, "")
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("google news: cursor blob is not valid JSON, starting fresh")
        return {}
    return payload if isinstance(payload, dict) else {}


def save_cursors(session: Optional[Session], cursors: Dict[str, str]) -> None:
    if session is None:
        return
    set_setting(session, CURSOR_SETTING_KEY, json.dumps(cursors, ensure_ascii=False))


def _entry_datetime(entry) -> Optional[datetime]:
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    return datetime(*parsed[:6], tzinfo=timezone.utc) if parsed else None


def _entry_source(entry) -> str:
    source = entry.get("source", {})
    return str(source.get("title", "")).strip() if isinstance(source, dict) else ""


def parse_feed_entries(
    content: str,
    *,
    source_name: str,
    cutoff: datetime,
    max_items: int,
) -> List[Dict]:
    entries = []
    for item in feedparser.parse(content).entries:
        published = _entry_datetime(item)
        # Every entry needs a timestamp: the cursor contract cannot be honoured
        # for undated items, so they are dropped rather than re-fetched forever.
        if published is None or published <= cutoff:
            continue
        publisher = _entry_source(item)
        body = item.get("summary", "") or item.get("description", "")
        parts = [body, f"Publisher: {publisher}" if publisher else ""]
        entries.append(
            {
                "title": item.get("title", "Untitled"),
                "link": item.get("link", ""),
                "published": published,
                "source": source_name,
                "content": "\n".join(part for part in parts if part),
                "summary": "",
            }
        )
        if len(entries) >= max_items:
            break
    return entries


def _enabled_pairs(params: Dict) -> List[tuple]:
    regions = params.get("regions") or list(REGIONS)
    topics = params.get("topics") or list(TOPICS)
    return [
        (region_id, topic_id)
        for region_id in regions
        if region_id in REGIONS
        for topic_id in topics
        if topic_id in TOPICS
    ]


async def fetch_google_news_entries(
    source: FetchSource,
    cutoff_time: datetime,
    session: Optional[Session] = None,
    *,
    now: Optional[datetime] = None,
) -> List[Dict]:
    """Fetch every configured region × topic feed.

    ``cutoff_time`` is deliberately unused: Google News is incremental by
    per-feed cursor, and the cycle cutoff is a 24h-wide window while the cursor
    may be minutes old. Re-applying the cutoff would drop the newest items on a
    short cycle, so the cursor plus ``max_lookback_hours`` alone decide what is
    new.
    """
    params = dict(source.params or {})
    timeout = int(params.get("request_timeout") or DEFAULT_TIMEOUT)
    max_items = max(1, int(params.get("max_items_per_feed") or DEFAULT_MAX_ITEMS))
    lookback_hours = min(
        MAX_LOOKBACK_HOURS, max(1, int(params.get("max_lookback_hours") or MAX_LOOKBACK_HOURS))
    )

    now = _utc(now or datetime.now(timezone.utc))
    oldest_allowed = now - timedelta(hours=lookback_hours)
    cursors = load_cursors(session)
    jobs = _enabled_pairs(params)
    if not jobs:
        return []

    async def fetch_one(client: aiohttp.ClientSession, region_id: str, topic_id: str):
        identifier = feed_id(region_id, topic_id)
        previous = _parse_cursor(cursors.get(identifier))
        cutoff = max(oldest_allowed, previous) if previous else oldest_allowed
        async with client.get(
            feed_url(region_id, topic_id), timeout=aiohttp.ClientTimeout(total=timeout)
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}")
            content = await response.text()
        return identifier, parse_feed_entries(
            content,
            source_name=source.title or feed_title(region_id, topic_id),
            cutoff=cutoff,
            max_items=max_items,
        )

    async with aiohttp.ClientSession(headers=DEFAULT_HEADERS, trust_env=True) as client:
        results = await asyncio.gather(
            *(fetch_one(client, region_id, topic_id) for region_id, topic_id in jobs),
            return_exceptions=True,
        )

    entries: List[Dict] = []
    updated = dict(cursors)
    for (region_id, topic_id), result in zip(jobs, results, strict=True):
        identifier = feed_id(region_id, topic_id)
        if isinstance(result, BaseException):
            logger.warning(
                "google news feed failed %s: %s", identifier, result or type(result).__name__
            )
            continue
        _, feed_entries = result
        entries.extend(feed_entries)
        updated[identifier] = now.isoformat()

    save_cursors(session, updated)
    return entries
