"""BrowserWorker-backed dynamic-page feed collection for slaver instances."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from src.adapters.crypto_crawler import CrawlError, CryptoCrawler
from src.db.models import FetchSource
from src.db.repositories import update_source_fingerprint

logger = logging.getLogger(__name__)

DEFAULT_DAEMON_PORT = 12321
SESSION_PREFIX = "news_agent_feed"


def _daemon_port() -> int:
    configured = os.environ.get("BrowserWorker_PORT", "").strip()
    if configured:
        try:
            return int(configured)
        except ValueError:
            logger.warning("invalid BrowserWorker_PORT=%r; using default port", configured)
    workspace_roots = [
        os.environ.get("MANAI_WORKSPACE_ROOT", "").strip(),
        os.environ.get("MANAI_HOME", "").strip(),
    ]
    lock_candidates = [
        Path(os.environ.get("BrowserWorker_LOCK_FILE", "")),
        Path.home() / ".BrowserWorker" / "BrowserWorker.lock",
    ]
    lock_candidates.extend(
        Path(root) / "storage" / "browser_daemon.lock"
        for root in workspace_roots
        if root
    )
    lock_candidates.append(
        Path(__file__).resolve().parents[3]
        / "manai_worker"
        / "storage"
        / "browser_daemon.lock"
    )
    for lock_file in lock_candidates:
        if lock_file.is_file():
            try:
                data = json.loads(lock_file.read_text(encoding="utf-8"))
                return int(data.get("port", DEFAULT_DAEMON_PORT))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                logger.warning("could not read BrowserWorker lock file: %s", lock_file)
    return DEFAULT_DAEMON_PORT


def _daemon_request(
    path: str,
    payload: dict | None = None,
    timeout: int = 5,
    port: int | None = None,
) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port or _daemon_port()}{path}",
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"BrowserWorker {path} request failed: {exc}") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"BrowserWorker {path} returned a non-object response")
    return result


def _acquire_browser_lock(session_id: str, domains: Sequence[str], port: int) -> None:
    try:
        response = _daemon_request(
            "/lock/acquire",
            {
                "session_id": session_id,
                "skill": "news-agent-feed",
                "domains": list(domains),
                "concurrency_policy": "domain",
            },
            port=port,
        )
    except Exception as exc:
        raise RuntimeError(f"BrowserWorker domain lock request failed: {exc}") from exc
    if response.get("status") == "busy":
        raise RuntimeError(response.get("message") or "BrowserWorker domain is busy")
    if response.get("status") != "success":
        raise RuntimeError(f"BrowserWorker lock acquisition failed: {response}")


def _release_browser_lock(session_id: str, port: int) -> None:
    try:
        _daemon_request("/lock/release", {"session_id": session_id}, port=port)
    except Exception as exc:
        logger.warning("BrowserWorker lock release failed session=%s error=%s", session_id, exc)


def _session_id(source: FetchSource) -> str:
    instance = os.environ.get("NEWS_AGENT_INSTANCE_ID") or "local"
    identity = f"{instance}:{source.owner}:{source.key}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"{SESSION_PREFIX}_{digest}"


def fetch_feed_source(
    source: FetchSource,
    since_time: datetime,
) -> List[Dict]:
    params = dict(source.params or {})
    url = str(params.get("url") or "").strip()
    if not url:
        raise ValueError("missing params.url")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"invalid feed page URL: {url}")

    session_id = _session_id(source)
    domains = [parsed.netloc.lower()]
    crawler = CryptoCrawler(
        session_id=session_id,
        persist_store=False,
    )
    daemon_port = crawler.cdp.daemon_port
    started_at = datetime.now(timezone.utc)
    logger.info(
        "feed fetch started source=%s key=%s url=%s since=%s session=%s",
        source.title,
        source.key,
        url,
        since_time.isoformat(),
        session_id,
    )
    _acquire_browser_lock(session_id, domains, daemon_port)
    try:
        result = crawler.crawl_url(
            url,
            since_time=since_time,
            max_hours=24,
            persist=False,
        )
        if not result.get("success"):
            raise CrawlError(
                str(result.get("message") or "feed crawler failed"),
                error_type=str(result.get("error_type") or "FEED_CRAWL_FAILED"),
            )
        entries: List[Dict] = []
        for item in result.get("items") or []:
            published_at = item.get("published_at")
            published = (
                datetime.fromtimestamp(float(published_at), timezone.utc)
                if published_at is not None
                else None
            )
            title = str(item.get("title") or "").strip()
            content = str(item.get("content") or "").strip()
            link = str(item.get("url") or "").strip()
            if not title:
                logger.warning(
                    "feed item missing title source=%s key=%s has_url=%s",
                    source.title,
                    source.key,
                    bool(link),
                )
                continue
            entries.append(
                {
                    "title": title,
                    "link": link,
                    "published": published,
                    "source": source.title,
                    "content": content,
                    "summary": content,
                }
            )
        logger.info(
            "feed fetch completed source=%s key=%s fetched=%d elapsed_seconds=%.1f",
            source.title,
            source.key,
            len(entries),
            (datetime.now(timezone.utc) - started_at).total_seconds(),
        )
        return entries
    finally:
        _release_browser_lock(session_id, daemon_port)


async def fetch_feed_sources(
    sources: Sequence[FetchSource],
    cutoff_time: datetime,
    session: Optional[Session] = None,
    *,
    is_master: bool = True,
) -> Dict[str, List[Dict]]:
    """Run fixed Feed pages sequentially; master never invokes the browser."""
    selected = [
        source
        for source in sources
        if source.enabled and source.kind == "feed" and not is_master
    ]
    if not selected:
        return {}

    collected: Dict[str, List[Dict]] = {}
    for source in selected:
        last_fetched_at = source.last_fetched_at
        if last_fetched_at is not None and last_fetched_at.tzinfo is None:
            last_fetched_at = last_fetched_at.replace(tzinfo=timezone.utc)
        since_time = max(cutoff_time, last_fetched_at) if last_fetched_at else cutoff_time
        since_time = min(since_time, datetime.now(timezone.utc))
        fetch_started_at = datetime.now(timezone.utc)
        try:
            collected[source.id] = await asyncio.to_thread(
                fetch_feed_source, source, since_time
            )
            if session is not None:
                update_source_fingerprint(
                    session,
                    source.id,
                    fetched=True,
                    fetched_at=fetch_started_at,
                )
        except Exception:
            if session is not None:
                update_source_fingerprint(session, source.id, error=True)
            logger.exception(
                "feed fetch failed; source skipped source=%s key=%s url=%s",
                source.title,
                source.key,
                str((source.params or {}).get("url") or ""),
            )
    return collected
