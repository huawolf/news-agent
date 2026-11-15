"""Hacker News adapter (``kind = hackernews``).

Emits unified entries: frontpage stories enriched with the Algolia comment tree
and the linked article body. There is no separate "select the AI-relevant ones"
LLM step any more — relevance is decided once, by the scoring stage, like every
other source. ``select_k`` now simply caps how many frontpage stories get the
expensive enrich treatment per cycle.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from src.db.models import FetchSource
from src.sections.hackernews.frontpage_scraper import (
    fetch_frontpage,
    parse_frontpage_html,
)
from src.sections.hackernews.item_enricher import enrich_stories

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10
DEFAULT_SELECT_K = 3
DEFAULT_ALGOLIA_BASE = "https://hn.algolia.com/api/v1"
DEFAULT_TOKEN_NAME = "JINA_API_KEY"


def _params(source: FetchSource) -> Dict:
    return dict(source.params or {})


def _story_content(story: Dict) -> str:
    parts = []
    link_content = (story.get("link_content") or "").strip()
    if link_content:
        parts.append(link_content)

    comments = story.get("top_comments") or []
    if comments:
        lines = []
        for item in comments:
            lines.append(f"- {item['l1']}")
            for reply in item.get("replies") or []:
                lines.append(f"  - {reply}")
        parts.append("Top comments:\n" + "\n".join(lines))

    meta = f"Points: {story.get('points', 0)} | Comments: {story.get('comments', 0)}"
    if story.get("site"):
        meta = f"Site: {story['site']} | " + meta
    parts.append(meta)
    return "\n\n".join(parts)


def _story_published(story: Dict) -> datetime:
    """HN frontpage rows carry no timestamp; the enrich pass has none either.

    The story is being served on the frontpage right now, so "now" is the honest
    answer and keeps the 24h freshness filter meaningful.
    """
    return datetime.now(timezone.utc)


def _to_entry(story: Dict, source_name: str) -> Optional[Dict]:
    title = (story.get("title") or "").strip()
    link = (story.get("url") or "").strip()
    if not title or not link:
        return None
    return {
        "title": title,
        "link": link,
        "published": _story_published(story),
        "source": source_name,
        "content": _story_content(story),
        "summary": "",
    }


async def fetch_hackernews_entries(
    source: FetchSource,
    cutoff_time: datetime,
    session: Optional[Session] = None,
) -> List[Dict]:
    params = _params(source)
    timeout = int(params.get("request_timeout") or DEFAULT_TIMEOUT)
    select_k = max(1, int(params.get("select_k") or DEFAULT_SELECT_K))
    algolia_base = params.get("algolia_base") or DEFAULT_ALGOLIA_BASE
    token_name = params.get("token_name") or DEFAULT_TOKEN_NAME

    html = await fetch_frontpage(timeout=timeout)
    stories = parse_frontpage_html(html)
    if not stories:
        logger.info("hackernews: frontpage returned no stories")
        return []

    stories = stories[:select_k]
    enriched, errors = await enrich_stories(
        stories,
        top_comments=int(params.get("top_comments") or 50),
        top_l2_per_l1=int(params.get("top_l2_per_l1") or 3),
        comment_max_chars=int(params.get("comment_max_chars") or 2000),
        comments_total_chars=int(params.get("comments_total_chars") or 80000),
        link_content_max_chars=int(params.get("link_content_max_chars") or 50000),
        algolia_base=algolia_base,
        timeout=timeout,
        jina_token_env=token_name,
    )
    for message in errors:
        logger.warning("hackernews enrich failed: %s", message)

    source_name = source.title or "Hacker News"
    entries = []
    for story in enriched:
        entry = _to_entry(story, source_name)
        if entry is None:
            continue
        published = entry["published"]
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        if published < cutoff_time:
            continue
        entries.append(entry)
    return entries