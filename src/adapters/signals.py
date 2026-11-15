"""Built-in community / ranking signal adapter (``kind = signals``).

The per-source switches stay inside ``params["sources"]`` rather than becoming
``fetch_source`` rows (§4.1): the adapter expands and requests them itself, so
splitting them out would leave the same switch stored in two places.

The old six-value ``source_categories`` taxonomy is gone. Sector attribution is
decided once by the scoring stage from the ``sector`` table.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

import aiohttp
import feedparser
from sqlalchemy.orm import Session

from src.db.models import FetchSource

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

GITHUB_VARIANTS = {
    "github-trending": ("GitHub Trending", "https://github.com/trending?since=daily"),
    "github-trending-js": ("GitHub Trending JS", "https://github.com/trending/javascript?since=daily"),
    "github-trending-zh": ("GitHub Trending Chinese", "https://github.com/trending?since=daily&spoken_language_code=zh"),
}

RSS_FEEDS = {
    "36kr": ("36Kr", "https://36kr.com/feed"),
    "sspai": ("Sspai", "https://sspai.com/feed"),
    "oschina": ("OSChina", "https://www.oschina.net/news/rss"),
}

JIKE_TOPICS = {
    "jike-ai-explore": ("Jike AI Explore", "63579abb6724cc583b9bba9a"),
    "jike-ai-discuss": ("Jike AI Discussion", "55fadac08cc2e30e00e2e42a"),
    "jike-engineer": ("Jike Engineers", "577c5a122fa95b1100da059f"),
}

V2EX_NODES = ("create", "share", "programmer")

APPSTORE_REGIONS = {
    "appstore-cn": ("App Store China", "cn"),
    "appstore-tw": ("App Store Taiwan", "tw"),
    "appstore-us": ("App Store US", "us"),
    "appstore-jp": ("App Store Japan", "jp"),
    "appstore-kr": ("App Store Korea", "kr"),
}

REDDIT_SUBREDDITS = ("SideProject", "SaaS", "startups", "Entrepreneur")
PRODUCTHUNT_GRAPHQL_URL = "https://api.producthunt.com/v2/api/graphql"

DEFAULT_TIMEOUT = 10
DEFAULT_MAX_ITEMS = 20
DEFAULT_RSSHUB_BASE = "https://rsshub.umzzz.com"
DEFAULT_PH_TOKEN_ENV = "PH_TOKEN"

ALL_SOURCE_IDS = (
    tuple(GITHUB_VARIANTS)
    + tuple(RSS_FEEDS)
    + tuple(JIKE_TOPICS)
    + ("v2ex",)
    + tuple(APPSTORE_REGIONS)
    + ("producthunt", "reddit")
)


def enabled_source_ids(params: Dict) -> set:
    """Enabled sub-source ids; an absent map means every known sub-source."""
    switches = params.get("sources")
    if not isinstance(switches, dict) or not switches:
        return set(ALL_SOURCE_IDS)
    return {key for key, value in switches.items() if value}


def _parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(text).astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError):
        return None


def _entry(
    *,
    title: str,
    link: str,
    source: str,
    content: str = "",
    published: Optional[datetime] = None,
) -> Optional[Dict]:
    title = (title or "").strip()
    link = (link or "").strip()
    if not title or not link:
        return None
    return {
        "title": title,
        "link": link,
        "published": published or datetime.now(timezone.utc),
        "source": source,
        "content": content or "",
        "summary": "",
    }


def _is_fresh(published: Optional[datetime], cutoff_time: Optional[datetime]) -> bool:
    if not cutoff_time:
        return True
    if not published:
        return False
    pub = published.astimezone(timezone.utc) if published.tzinfo else published.replace(tzinfo=timezone.utc)
    cutoff = cutoff_time.astimezone(timezone.utc) if cutoff_time.tzinfo else cutoff_time.replace(tzinfo=timezone.utc)
    return pub >= cutoff


async def _get_text(session: aiohttp.ClientSession, url: str, timeout: int) -> str:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        return await resp.text()


async def _get_json(session: aiohttp.ClientSession, url: str, timeout: int, **kwargs) -> Dict:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout), **kwargs) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        return await resp.json(content_type=None)


def _parse_github_trending(html: str, source_name: str) -> List[Dict]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    entries = []
    now = datetime.now(timezone.utc)
    for article in soup.select("article.Box-row"):
        repo_link = article.select_one("h2 a")
        if not repo_link:
            continue
        repo_path = repo_link.get("href", "").strip("/")
        if not repo_path or "/" not in repo_path:
            continue
        desc_el = article.select_one("p")
        language_el = article.select_one('[itemprop="programmingLanguage"]')
        today_el = article.select_one(".d-inline-block.float-sm-right")
        content = "\n".join(
            part
            for part in [
                desc_el.get_text(" ", strip=True) if desc_el else "",
                f"Language: {language_el.get_text(strip=True)}" if language_el else "",
                " ".join(today_el.get_text(" ", strip=True).split()) if today_el else "",
            ]
            if part
        )
        item = _entry(
            title=repo_path,
            link=f"https://github.com/{repo_path}",
            source=source_name,
            content=content,
            published=now,
        )
        if item:
            entries.append(item)
    return entries


async def _fetch_github_variant(
    session: aiohttp.ClientSession,
    name: str,
    url: str,
    timeout: int,
    max_items: int,
) -> List[Dict]:
    html = await _get_text(session, url, timeout)
    return _parse_github_trending(html, name)[:max_items]


async def _fetch_feed(
    session: aiohttp.ClientSession,
    source_name: str,
    url: str,
    timeout: int,
    max_items: int,
    cutoff_time: Optional[datetime],
) -> List[Dict]:
    payload = await _get_text(session, url, timeout)
    entries = []
    for feed_entry in feedparser.parse(payload).entries[:max_items]:
        published = None
        if getattr(feed_entry, "published_parsed", None):
            published = datetime(*feed_entry.published_parsed[:6], tzinfo=timezone.utc)
        elif getattr(feed_entry, "updated_parsed", None):
            published = datetime(*feed_entry.updated_parsed[:6], tzinfo=timezone.utc)
        if not _is_fresh(published, cutoff_time):
            continue
        body = ""
        if getattr(feed_entry, "content", None):
            body = feed_entry.content[0].get("value", "")
        body = body or getattr(feed_entry, "description", "") or getattr(feed_entry, "summary", "")
        item = _entry(
            title=feed_entry.get("title", ""),
            link=feed_entry.get("link", ""),
            source=source_name,
            content=body,
            published=published,
        )
        if item:
            entries.append(item)
    return entries


async def _fetch_appstore_region(
    session: aiohttp.ClientSession,
    source_name: str,
    region_code: str,
    timeout: int,
    max_items: int,
    cutoff_time: Optional[datetime],
) -> List[Dict]:
    url = f"https://itunes.apple.com/{region_code}/rss/newapplications/limit={max_items}/json"
    data = await _get_json(session, url, timeout)
    entries = []
    for item in data.get("feed", {}).get("entry", [])[:max_items]:
        name = (item.get("im:name", {}) or {}).get("label", "")
        app_id = (item.get("id", {}) or {}).get("attributes", {}).get("im:id", "")
        link = (item.get("link", {}) or {}).get("attributes", {}).get("href", "")
        published = _parse_datetime((item.get("im:releaseDate", {}) or {}).get("label"))
        if not _is_fresh(published, cutoff_time):
            continue
        category = (item.get("category", {}) or {}).get("attributes", {}).get("label", "")
        artist = (item.get("im:artist", {}) or {}).get("label", "")
        price = (item.get("im:price", {}) or {}).get("label", "")
        lookup = {}
        if app_id:
            try:
                lookup_data = await _get_json(
                    session,
                    "https://itunes.apple.com/lookup",
                    timeout,
                    params={"id": app_id, "country": region_code},
                )
                results = lookup_data.get("results", [])
                lookup = results[0] if results else {}
            except Exception as exc:
                logger.warning("App Store lookup %s/%s: %s", region_code, app_id, exc)
        description = (lookup.get("description") or "")[:1200]
        rating = lookup.get("averageUserRating")
        ratings_count = lookup.get("userRatingCount")
        genre = lookup.get("primaryGenreName") or category
        content = " | ".join(
            part
            for part in [
                "Signal Type: App Store new app release",
                f"Category: {genre}" if genre else "",
                f"Developer: {artist}" if artist else "",
                f"Price: {price}" if price else "",
                f"Rating: {rating} ({ratings_count} ratings)" if rating and ratings_count else "",
            ]
            if part
        )
        if description:
            content = f"{content}\nDescription: {description}"
        entry = _entry(
            title=name,
            link=link or f"https://apps.apple.com/{region_code}/app/id{app_id}",
            source=source_name,
            content=content,
            published=published,
        )
        if entry:
            entries.append(entry)
    return entries


async def _fetch_v2ex(
    session: aiohttp.ClientSession,
    timeout: int,
    max_items: int,
    cutoff_time: Optional[datetime],
) -> List[Dict]:
    entries = []
    seen_ids = set()
    per_node = max(1, max_items // len(V2EX_NODES))
    for node in V2EX_NODES:
        try:
            data = await _get_json(
                session,
                "https://www.v2ex.com/api/topics/show.json",
                timeout,
                params={"node_name": node},
            )
        except Exception as exc:
            logger.warning("V2EX %s: %s", node, exc)
            continue
        for topic in data[:per_node]:
            topic_id = str(topic.get("id", ""))
            if not topic_id or topic_id in seen_ids:
                continue
            seen_ids.add(topic_id)
            published = _parse_datetime(topic.get("created"))
            if not _is_fresh(published, cutoff_time):
                continue
            node_name = (topic.get("node") or {}).get("title", node)
            content = "\n".join(
                part
                for part in [
                    (topic.get("content") or "")[:1200],
                    f"Node: {node_name}",
                    f"Replies: {topic.get('replies', 0)}",
                ]
                if part
            )
            item = _entry(
                title=topic.get("title", ""),
                link=f"https://www.v2ex.com/t/{topic_id}",
                source="V2EX",
                content=content,
                published=published,
            )
            if item:
                entries.append(item)
    return entries[:max_items]


async def _fetch_producthunt(
    session: aiohttp.ClientSession,
    timeout: int,
    max_items: int,
    cutoff_time: Optional[datetime],
    token_env: str = "PH_TOKEN",
) -> List[Dict]:
    token = os.environ.get(token_env, "").strip()
    if not token:
        logger.info("Product Hunt: %s not set; skipping", token_env)
        return []
    posted_after = ""
    if cutoff_time:
        cutoff = cutoff_time.astimezone(timezone.utc) if cutoff_time.tzinfo else cutoff_time.replace(tzinfo=timezone.utc)
        posted_after = f'postedAfter: "{cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")}", '
    query = """
    query {
      posts(%sfirst: %d, order: RANKING) {
        nodes {
          name
          tagline
          description
          votesCount
          commentsCount
          url
          website
          featuredAt
          topics { nodes { name } }
        }
      }
    }
    """ % (posted_after, max_items * 3)
    async with session.post(
        PRODUCTHUNT_GRAPHQL_URL,
        headers={"Authorization": f"Bearer {token}"},
        json={"query": query},
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as resp:
        payload = await resp.json(content_type=None)
        if resp.status != 200 or payload.get("errors"):
            raise RuntimeError(f"Product Hunt API error: {payload.get('errors') or resp.status}")
    entries = []
    for post in payload.get("data", {}).get("posts", {}).get("nodes", []):
        published = _parse_datetime(post.get("featuredAt"))
        if not _is_fresh(published, cutoff_time):
            continue
        topics = [t.get("name", "") for t in post.get("topics", {}).get("nodes", []) if t.get("name")]
        content = "\n".join(
            part
            for part in [
                post.get("tagline", ""),
                post.get("description", ""),
                f"Votes: {post.get('votesCount', 0)} | Comments: {post.get('commentsCount', 0)}",
                f"Website: {post.get('website', '')}" if post.get("website") else "",
                f"Topics: {', '.join(topics)}" if topics else "",
            ]
            if part
        )
        item = _entry(
            title=post.get("name", ""),
            link=post.get("url", ""),
            source="Product Hunt",
            content=content,
            published=published,
        )
        if item:
            entries.append(item)
    return entries[:max_items]


def _parse_reddit_post(
    post: Dict[str, Any], subreddit: str, cutoff_time: Optional[datetime]
) -> Optional[Dict]:
    published = _parse_datetime(post.get("created_utc"))
    if not _is_fresh(published, cutoff_time):
        return None
    permalink = post.get("permalink", "")
    url = post.get("url") or (f"https://www.reddit.com{permalink}" if permalink else "")
    content = "\n".join(
        part
        for part in [
            post.get("selftext", "")[:1200],
            f"Subreddit: r/{subreddit}",
            f"Score: {post.get('score', 0)} | Comments: {post.get('num_comments', 0)}",
        ]
        if part
    )
    return _entry(
        title=post.get("title", ""),
        link=url,
        source=f"Reddit r/{subreddit}",
        content=content,
        published=published,
    )


async def _fetch_reddit(
    session: aiohttp.ClientSession,
    timeout: int,
    max_items: int,
    cutoff_time: Optional[datetime],
) -> List[Dict]:
    """Pullpush first, official Reddit JSON as the fallback."""
    per_subreddit = max(1, max_items // len(REDDIT_SUBREDDITS))
    entries = []
    for subreddit in REDDIT_SUBREDDITS:
        try:
            data = await _get_json(
                session,
                "https://api.pullpush.io/reddit/search/submission/",
                timeout,
                params={"subreddit": subreddit, "size": per_subreddit},
            )
        except Exception as exc:
            logger.warning("Reddit pullpush r/%s: %s", subreddit, exc)
            continue
        for post in data.get("data", []):
            item = _parse_reddit_post(post, subreddit, cutoff_time)
            if item:
                entries.append(item)
    if entries:
        return entries[:max_items]

    for subreddit in REDDIT_SUBREDDITS:
        try:
            data = await _get_json(
                session,
                f"https://www.reddit.com/r/{subreddit}/new.json?limit={per_subreddit}",
                timeout,
            )
        except Exception as exc:
            logger.warning("Reddit JSON r/%s: %s", subreddit, exc)
            continue
        for child in data.get("data", {}).get("children", []):
            item = _parse_reddit_post(child.get("data", {}), subreddit, cutoff_time)
            if item:
                entries.append(item)
    return entries[:max_items]


async def fetch_signal_entries(
    source: FetchSource,
    cutoff_time: datetime,
    session: Optional[Session] = None,
) -> List[Dict]:
    params = dict(source.params or {})
    enabled = enabled_source_ids(params)
    timeout = int(params.get("request_timeout") or DEFAULT_TIMEOUT)
    max_items = max(1, int(params.get("max_items_per_source") or DEFAULT_MAX_ITEMS))
    rsshub_base = params.get("rsshub_base") or os.environ.get("RSSHUB_BASE") or DEFAULT_RSSHUB_BASE
    ph_token_env = params.get("producthunt_token_env") or DEFAULT_PH_TOKEN_ENV

    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8,zh-CN;q=0.6"}
    jobs = []
    async with aiohttp.ClientSession(headers=headers, trust_env=True) as client:
        for key, (name, url) in GITHUB_VARIANTS.items():
            if key in enabled:
                jobs.append((key, _fetch_github_variant(client, name, url, timeout, max_items)))
        for key, (name, url) in RSS_FEEDS.items():
            if key in enabled:
                jobs.append((key, _fetch_feed(client, name, url, timeout, max_items, cutoff_time)))
        for key, (name, topic_id) in JIKE_TOPICS.items():
            if key in enabled:
                jobs.append(
                    (
                        key,
                        _fetch_feed(
                            client,
                            name,
                            f"{rsshub_base.rstrip('/')}/jike/topic/{topic_id}",
                            timeout,
                            max_items,
                            cutoff_time,
                        ),
                    )
                )
        if "v2ex" in enabled:
            jobs.append(("v2ex", _fetch_v2ex(client, timeout, max_items, cutoff_time)))
        for key, (name, region_code) in APPSTORE_REGIONS.items():
            if key in enabled:
                jobs.append(
                    (key, _fetch_appstore_region(client, name, region_code, timeout, max_items, cutoff_time))
                )
        if "producthunt" in enabled:
            jobs.append(
                (
                    "producthunt",
                    _fetch_producthunt(
                        client, timeout, max_items, cutoff_time, token_env=ph_token_env
                    ),
                )
            )
        if "reddit" in enabled:
            jobs.append(("reddit", _fetch_reddit(client, timeout, max_items, cutoff_time)))

        if not jobs:
            return []

        results = await asyncio.gather(*(task for _, task in jobs), return_exceptions=True)

    entries = []
    for (source_id, _), result in zip(jobs, results, strict=True):
        if isinstance(result, BaseException):
            logger.warning(
                "signal source failed %s: %s", source_id, result or type(result).__name__
            )
            continue
        entries.extend(result)

    seen_links = set()
    unique = []
    for entry in entries:
        link = entry.get("link")
        if not link or link in seen_links:
            continue
        seen_links.add(link)
        unique.append(entry)
    return unique
