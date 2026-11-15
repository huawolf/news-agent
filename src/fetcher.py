"""Source fetching with HTTP conditional requests (§5.8).

Every source is a ``fetch_source`` row. RSS rows carry an ETag / Last-Modified
fingerprint that survives restarts, so an unchanged feed costs one ``304`` and no
XML parsing. The fingerprint is read from the row before the request and written
back after it by :func:`persist_outcomes`.

Two transport paths exist and both must send the conditional headers: plain RSS
over ``aiohttp``, and Nitter / xcancel over ``requests`` (public instances reject
the aiohttp TLS fingerprint). Fixing only the aiohttp path would leave every
Nitter source doing full downloads forever.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

import aiohttp
import feedparser
import requests

from src.db.models import FetchSource
from src.db.repositories import update_source_fingerprint

logger = logging.getLogger(__name__)

DEFAULT_FEED_TIMEOUT = 5

# nitter puts the whole post in <title>; keep the title readable.
TITLE_MAX_CHARS = 200

STATUS_OK = "ok"
STATUS_NOT_MODIFIED = "not_modified"
STATUS_ERROR = "error"

# nitter / xcancel instances: whitelisted UA + requests client. See
# knowledge/keypoints.txt — aiohttp's TLS fingerprint is rejected outright.
NITTER_HOSTS = (
    "xcancel.com",
    "nitter.net",
    "nuku.trabun.org",
    "nitter.privacyredirect.com",
)
NITTER_HEADERS = {
    "User-Agent": "Inoreader",
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9",
}
# Public instances: small pool plus a pause after each request.
NITTER_MAX_CONCURRENCY = 2
NITTER_REQUEST_DELAY = 1.0

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}


@dataclass
class FetchOutcome:
    """Result of one conditional fetch."""

    source: FetchSource
    status: str
    entries: List[Dict] = field(default_factory=list)
    etag: str = ""
    last_modified: str = ""
    error: str = ""

    @property
    def is_not_modified(self) -> bool:
        return self.status == STATUS_NOT_MODIFIED

    @property
    def is_ok(self) -> bool:
        return self.status == STATUS_OK


@dataclass
class HttpResponse:
    status: int
    body: bytes = b""
    etag: str = ""
    last_modified: str = ""


def is_nitter_url(url: str) -> bool:
    return any(host in (url or "") for host in NITTER_HOSTS)


def conditional_headers(etag: str = "", last_modified: str = "") -> Dict[str, str]:
    """Build the conditional request headers for a stored fingerprint.

    Both transports call this, so neither path can silently fall back to a full
    download.
    """
    headers: Dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    return headers


def _merge_headers(base: Dict[str, str], extra: Dict[str, str]) -> Dict[str, str]:
    headers = dict(base)
    headers.update(extra)
    return headers


def parse_entry_time(entry) -> Optional[datetime]:
    published_parsed = getattr(entry, "published_parsed", None)
    if published_parsed is not None:
        return datetime(*published_parsed[:6], tzinfo=timezone.utc)

    updated_parsed = getattr(entry, "updated_parsed", None)
    if updated_parsed is not None:
        return datetime(*updated_parsed[:6], tzinfo=timezone.utc)

    return None


def _extract_body(entry) -> str:
    content_list = getattr(entry, "content", None)
    if content_list:
        value = content_list[0].get("value", "")
        if value:
            return value
    description = getattr(entry, "description", "")
    if description:
        return description
    return getattr(entry, "summary", "") or ""


def _truncate_title(title: str) -> str:
    if len(title) <= TITLE_MAX_CHARS:
        return title
    return title[:TITLE_MAX_CHARS].rstrip() + "…"


def parse_feed_entries(content, source_name: str, cutoff_time: datetime) -> List[Dict]:
    """Parse a feed body into items, dropping anything published before cutoff."""
    feed = feedparser.parse(content)
    entries = []

    for entry in feed.entries:
        pub_date = parse_entry_time(entry)
        if pub_date and pub_date < cutoff_time:
            continue

        entries.append(
            {
                "title": _truncate_title(entry.get("title", "")),
                "link": entry.get("link", ""),
                "published": pub_date,
                "source": source_name,
                "content": _extract_body(entry),
                "summary": "",
            }
        )

    return entries


async def _request_aiohttp(
    url: str,
    headers: Dict[str, str],
    timeout: int,
    session: Optional[aiohttp.ClientSession] = None,
) -> HttpResponse:
    client_timeout = aiohttp.ClientTimeout(total=timeout)

    async def _do(sess: aiohttp.ClientSession) -> HttpResponse:
        async with sess.get(url, headers=headers, timeout=client_timeout) as resp:
            body = b""
            if resp.status == 200:
                body = await resp.read()
            return HttpResponse(
                status=resp.status,
                body=body,
                etag=resp.headers.get("ETag", "") or "",
                last_modified=resp.headers.get("Last-Modified", "") or "",
            )

    if session is not None:
        return await _do(session)
    async with aiohttp.ClientSession(trust_env=True) as sess:
        return await _do(sess)


async def _request_nitter(
    url: str, headers: Dict[str, str], timeout: int
) -> HttpResponse:
    def _sync() -> HttpResponse:
        response = requests.get(url, headers=headers, timeout=timeout)
        body = response.content if response.status_code == 200 else b""
        return HttpResponse(
            status=response.status_code,
            body=body,
            etag=response.headers.get("ETag", "") or "",
            last_modified=response.headers.get("Last-Modified", "") or "",
        )

    return await asyncio.to_thread(_sync)


async def fetch_source(
    source: FetchSource,
    cutoff_time: datetime,
    timeout: int = DEFAULT_FEED_TIMEOUT,
    session: Optional[aiohttp.ClientSession] = None,
) -> FetchOutcome:
    """Fetch one RSS source, honouring its stored fingerprint.

    ``304`` returns no entries and no new fingerprint: the body did not change,
    so there is nothing to parse and nothing to store. A ``429`` or ``5xx``
    likewise leaves the stored fingerprint untouched so the next cycle can
    retry with it.
    """
    url = (source.xml_url or "").strip()
    if not url:
        return FetchOutcome(source=source, status=STATUS_ERROR, error="missing xml_url")

    if timeout is None:
        timeout = DEFAULT_FEED_TIMEOUT

    conditional = conditional_headers(source.etag, source.last_modified)
    try:
        if is_nitter_url(url):
            response = await _request_nitter(
                url, _merge_headers(NITTER_HEADERS, conditional), timeout
            )
        else:
            response = await _request_aiohttp(
                url, _merge_headers(DEFAULT_HEADERS, conditional), timeout, session
            )
    except Exception as exc:  # transport failures keep the old fingerprint
        message = str(exc) or type(exc).__name__
        logger.warning("fetch failed source=%s url=%s error=%s", source.title, url, message)
        return FetchOutcome(source=source, status=STATUS_ERROR, error=message)

    if response.status == 304:
        logger.debug("304 not modified source=%s", source.title)
        return FetchOutcome(source=source, status=STATUS_NOT_MODIFIED)

    if response.status != 200:
        message = f"HTTP {response.status}"
        logger.warning("fetch failed source=%s url=%s %s", source.title, url, message)
        return FetchOutcome(source=source, status=STATUS_ERROR, error=message)

    entries = parse_feed_entries(response.body, source.title, cutoff_time)
    return FetchOutcome(
        source=source,
        status=STATUS_OK,
        entries=entries,
        etag=response.etag,
        last_modified=response.last_modified,
    )


async def fetch_all_sources(
    sources: Sequence[FetchSource],
    cutoff_time: datetime,
    max_workers: int = 10,
    timeout: int = DEFAULT_FEED_TIMEOUT,
    session: Optional[aiohttp.ClientSession] = None,
) -> List[FetchOutcome]:
    """Fetch every RSS source concurrently.

    Nitter sources get their own smaller pool and a pause afterwards so public
    instances are not hammered.
    """
    if timeout is None:
        timeout = DEFAULT_FEED_TIMEOUT

    rss_sources = list(sources)
    nitter_sources = [s for s in rss_sources if is_nitter_url(s.xml_url)]
    normal_sources = [s for s in rss_sources if not is_nitter_url(s.xml_url)]

    normal_sem = asyncio.Semaphore(max_workers)
    nitter_sem = asyncio.Semaphore(NITTER_MAX_CONCURRENCY)

    async def fetch_normal(source: FetchSource) -> FetchOutcome:
        async with normal_sem:
            return await fetch_source(source, cutoff_time, timeout, session)

    async def fetch_nitter(source: FetchSource) -> FetchOutcome:
        async with nitter_sem:
            try:
                return await fetch_source(source, cutoff_time, timeout, session)
            finally:
                await asyncio.sleep(NITTER_REQUEST_DELAY)

    # Nitter runs last so the ordinary pool drains first; the returned list is
    # re-ordered to match the input, so callers can zip outcomes with sources.
    tasks = [fetch_normal(s) for s in normal_sources] + [
        fetch_nitter(s) for s in nitter_sources
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    by_id: Dict[str, FetchOutcome] = {}
    for source, result in zip(normal_sources + nitter_sources, results, strict=True):
        if isinstance(result, BaseException):
            logger.warning(
                "fetch crashed source=%s error=%s", source.title, result or type(result).__name__
            )
            result = FetchOutcome(
                source=source,
                status=STATUS_ERROR,
                error=str(result) or type(result).__name__,
            )
        by_id[source.id] = result

    return [by_id[source.id] for source in rss_sources]


def persist_outcome(session, outcome: FetchOutcome) -> None:
    """Write one fetch result back to its ``fetch_source`` row.

    The stored fingerprint always mirrors what the server just said:
    ``200`` replaces it (and clears a validator the server stopped sending),
    ``304`` and transport errors leave it alone so the next cycle retries with
    the same fingerprint.
    """
    if outcome.status == STATUS_OK:
        update_source_fingerprint(
            session,
            outcome.source.id,
            etag=outcome.etag,
            last_modified=outcome.last_modified,
            fetched=True,
        )
        return

    update_source_fingerprint(
        session,
        outcome.source.id,
        fetched=outcome.is_not_modified,
        error=outcome.status == STATUS_ERROR,
    )


def persist_outcomes(session, outcomes: Sequence[FetchOutcome]) -> None:
    """Persist a whole fetch cycle's results."""
    for outcome in outcomes:
        persist_outcome(session, outcome)
