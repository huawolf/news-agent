"""GitHub Trending adapter (``kind = github_trending``).

Only the master instance runs this adapter; a slaver consumes the master's
``github_cache`` instead (§5.1, §5.6 step 8). Every entry carries an
``is_github`` marker plus the raw ``github`` payload; the fetch pipeline never
turns a repo into a signal — no dedup, no signal row — but it does rank the
deep-dive candidates with the shared ``score_batch`` prompt and keeps only the
top repos by score in the cache snapshot. The digest suppresses repeats via
its push-history window (``github_push``).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from src.db.models import FetchSource
from src.sections.github.repo_enricher import enrich_repos
from src.sections.github.trending_scraper import (
    fetch_trending_page,
    parse_trending_html,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10
DEFAULT_MAX_ITEMS = 3
DEFAULT_MAX_DEEP_DIVE = 10
DEFAULT_README_MAX_CHARS = 10000
DEFAULT_TOKEN_NAME = "GITHUB_TOKEN"


def _repo_content(repo: Dict) -> str:
    parts = []
    if repo.get("description"):
        parts.append(repo["description"])
    if repo.get("language"):
        parts.append(f"Language: {repo['language']}")
    if repo.get("stars_today"):
        parts.append(f"Stars today: {repo['stars_today']}")
    if repo.get("stars_total"):
        parts.append(f"Stars total: {repo['stars_total']}")
    if repo.get("topics"):
        parts.append(f"Topics: {', '.join(repo['topics'])}")
    if repo.get("license"):
        parts.append(f"License: {repo['license']}")
    if repo.get("pushed_at"):
        parts.append(f"Pushed at: {repo['pushed_at']}")
    readme = (repo.get("readme_excerpt") or "").strip()
    if readme:
        parts.append(readme)
    return "\n\n".join(parts)


def _to_entry(repo: Dict, max_items: int) -> Optional[Dict]:
    full_name = (repo.get("full_name") or "").strip()
    if not full_name:
        return None
    return {
        "title": full_name,
        "link": repo["url"],
        "published": datetime.now(timezone.utc),
        "source": "GitHub Trending",
        "content": _repo_content(repo),
        "summary": "",
        "is_github": True,
        "github": repo,
        # Internal pipeline hint; never rendered into the scoring prompt.
        "_github_max_items": max_items,
    }


async def fetch_github_trending_entries(
    source: FetchSource,
    cutoff_time: datetime,
    session: Optional[Session] = None,
) -> List[Dict]:
    params = dict(source.params or {})
    timeout = int(params.get("request_timeout") or DEFAULT_TIMEOUT)
    max_items = max(1, int(params.get("max_items") or DEFAULT_MAX_ITEMS))
    max_deep_dive = max(1, int(params.get("max_deep_dive") or DEFAULT_MAX_DEEP_DIVE))
    readme_max_chars = int(params.get("readme_max_chars") or DEFAULT_README_MAX_CHARS)
    token_name = params.get("token_name") or DEFAULT_TOKEN_NAME

    html = await fetch_trending_page(timeout=timeout)
    repos = parse_trending_html(html)
    if not repos:
        logger.info("github trending: page returned no repos")
        return []

    # The deep dive costs two REST calls per repo, so it only runs on the
    # leading slice. Every enriched repo is returned and the pipeline keeps
    # only ``params.max_items`` best-scoring ones in the snapshot.
    repos = repos[:max_deep_dive]
    enriched, errors = await enrich_repos(
        repos,
        token_env=token_name,
        readme_max_chars=readme_max_chars,
        timeout=timeout,
    )
    for message in errors:
        logger.warning("github enrich failed: %s", message)

    entries = []
    for repo in enriched:
        entry = _to_entry(repo, max_items)
        if entry is not None:
            entries.append(entry)
    return entries
