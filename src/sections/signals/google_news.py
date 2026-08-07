"""Google News topic RSS collector with per-feed incremental cursors."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import aiohttp
import feedparser


REGIONS = {
    "cn": {"label": "China", "hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"},
    "us": {"label": "United States", "hl": "en-US", "gl": "US", "ceid": "US:en"},
}

TOPICS = {
    "business": {"topic": "BUSINESS", "category": "business_investment"},
    "technology": {"topic": "TECHNOLOGY", "category": "technology_policy"},
    "science": {"topic": "SCIENCE", "category": "other"},
}

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NewsAgent/0.2; +https://github.com/)",
    "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
}


def google_news_source_catalog() -> List[Dict[str, str]]:
    """Return the configured Google News topic endpoints for API/UI display."""
    sources = []
    for region_id, region in REGIONS.items():
        for topic_id, topic in TOPICS.items():
            source_id = f"google-news-{region_id}-{topic_id}"
            sources.append(
                {
                    "id": source_id,
                    "name": f"Google News {region['label']} {topic['topic'].title()}",
                    "xmlUrl": _feed_url(region_id, topic_id),
                    "kind": "signal",
                    "category": topic["category"],
                }
            )
    return sources


def _feed_url(region_id: str, topic_id: str) -> str:
    region = REGIONS[region_id]
    topic = TOPICS[topic_id]["topic"]
    return (
        f"https://news.google.com/rss/headlines/section/topic/{topic}"
        f"?hl={region['hl']}&gl={region['gl']}&ceid={region['ceid']}"
    )


def _parse_cursor(value: object) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _load_cursors(path: Path) -> Dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    feeds = payload.get("feeds", {}) if isinstance(payload, dict) else {}
    return feeds if isinstance(feeds, dict) else {}


def _save_cursors(path: Path, cursors: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps({"feeds": cursors}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


def _entry_datetime(entry) -> Optional[datetime]:
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    return datetime(*parsed[:6], tzinfo=timezone.utc) if parsed else None


def _entry_source(entry) -> str:
    source = entry.get("source", {})
    if isinstance(source, dict):
        return str(source.get("title", "")).strip()
    return ""


def _parse_entries(
    content: str,
    *,
    source_name: str,
    region_id: str,
    topic_id: str,
    cutoff: datetime,
    max_items: int,
) -> List[Dict]:
    feed = feedparser.parse(content)
    entries = []
    topic = TOPICS[topic_id]
    for item in feed.entries:
        published = _entry_datetime(item)
        # Incremental feeds require a timestamp; accepting undated entries would
        # make the cursor contract impossible to enforce.
        if published is None or published <= cutoff:
            continue
        publisher = _entry_source(item)
        content_parts = [item.get("summary", "") or item.get("description", "")]
        if publisher:
            content_parts.append(f"Publisher: {publisher}")
        entries.append(
            {
                "title": item.get("title", "Untitled"),
                "link": item.get("link", ""),
                "published": published,
                "source": source_name,
                "content": "\n".join(part for part in content_parts if part),
                "tags": ["Google News", REGIONS[region_id]["label"], topic["topic"].title()],
                "category": topic["category"],
                "google_news_region": region_id,
                "google_news_topic": topic_id,
                "score": 0,
                "summary": "",
            }
        )
        if len(entries) >= max_items:
            break
    return entries


async def fetch_google_news_entries(
    config: Dict,
    *,
    now: Optional[datetime] = None,
) -> List[Dict]:
    """Fetch enabled country/topic feeds since their last successful request."""
    cfg = config.get("sections", {}).get("google_news", {})
    if not cfg.get("enabled", False):
        return []

    current = now or datetime.now(timezone.utc)
    current = current.astimezone(timezone.utc) if current.tzinfo else current.replace(tzinfo=timezone.utc)
    max_lookback_hours = min(24, max(1, int(cfg.get("max_lookback_hours", 24))))
    oldest_allowed = current - timedelta(hours=max_lookback_hours)
    max_items = max(1, int(cfg.get("max_items_per_feed", 20)))
    timeout = int(cfg.get("request_timeout", config.get("fetch", {}).get("timeout", 10)))
    enabled_regions = [value for value in cfg.get("regions", ["cn", "us"]) if value in REGIONS]
    enabled_topics = [value for value in cfg.get("topics", ["business", "technology", "science"]) if value in TOPICS]
    source_overrides = config.get("sections", {}).get("signals", {}).get("sources", {})

    data_dir = Path(config.get("storage", {}).get("data_dir", "news-data"))
    state_path = data_dir / "google-news-state.json"
    cursors = _load_cursors(state_path)

    async def fetch_one(session: aiohttp.ClientSession, region_id: str, topic_id: str):
        source_id = f"google-news-{region_id}-{topic_id}"
        previous = _parse_cursor(cursors.get(source_id))
        cutoff = max(oldest_allowed, previous) if previous else oldest_allowed
        url = _feed_url(region_id, topic_id)
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}")
            content = await response.text()
        source_name = f"Google News {REGIONS[region_id]['label']} {TOPICS[topic_id]['topic'].title()}"
        return source_id, _parse_entries(
            content,
            source_name=source_name,
            region_id=region_id,
            topic_id=topic_id,
            cutoff=cutoff,
            max_items=max_items,
        )

    jobs = [
        (region_id, topic_id)
        for region_id in enabled_regions
        for topic_id in enabled_topics
        if source_overrides.get(f"google-news-{region_id}-{topic_id}", True)
    ]
    if not jobs:
        return []

    async with aiohttp.ClientSession(headers=DEFAULT_HEADERS, trust_env=True) as session:
        results = await asyncio.gather(
            *(fetch_one(session, region_id, topic_id) for region_id, topic_id in jobs),
            return_exceptions=True,
        )

    entries = []
    cursor_changed = False
    for (region_id, topic_id), result in zip(jobs, results):
        source_id = f"google-news-{region_id}-{topic_id}"
        if isinstance(result, Exception):
            print(f"⚠️ Google News source failed {source_id}: {result}")
            continue
        _, feed_entries = result
        entries.extend(feed_entries)
        cursors[source_id] = current.isoformat()
        cursor_changed = True

    if cursor_changed:
        _save_cursors(state_path, cursors)
    return entries
