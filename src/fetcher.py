"""RSS抓取模块"""

import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Optional

import aiohttp
import feedparser
import requests

# 默认超时配置（秒）
DEFAULT_FEED_TIMEOUT = 5

# title 截断阈值：nitter 会把整条推文塞进 <title>，需要截断
TITLE_MAX_CHARS = 200

# nitter / xcancel 实例：必须用白名单 UA + requests 客户端（aiohttp 的 TLS
# 指纹过不了），详见 nitter-practice.md
NITTER_HOSTS = (
    "xcancel.com",
    "nitter.net",
    "nuku.trabun.org",
)
NITTER_HEADERS = {
    "User-Agent": "Inoreader",
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9",
}
# 公益实例，独立的低并发池 + 每次抓完 sleep，避免给上游施压
NITTER_MAX_CONCURRENCY = 2
NITTER_REQUEST_DELAY = 1.0

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}


def is_nitter_url(url: str) -> bool:
    """判断是否为 nitter / xcancel 实例 URL"""
    return any(host in url for host in NITTER_HOSTS)


def parse_entry_time(entry) -> Optional[datetime]:
    """解析条目的发布时间 (返回带 UTC 时区的 datetime)"""
    published_parsed = getattr(entry, "published_parsed", None)
    if published_parsed is not None:
        return datetime(*published_parsed[:6], tzinfo=timezone.utc)

    updated_parsed = getattr(entry, "updated_parsed", None)
    if updated_parsed is not None:
        return datetime(*updated_parsed[:6], tzinfo=timezone.utc)

    return None


def _extract_body(entry) -> str:
    """提取条目正文：优先 content（含 <content:encoded>），其次 description，最后 summary"""
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


def _parse_feed_entries(content, feed_info: Dict, cutoff_time: datetime) -> List[Dict]:
    """把 feed 字节/字符串解析为条目列表，按 cutoff 时间过滤"""
    feed = feedparser.parse(content)
    entries = []

    for entry in feed.entries:
        pub_date = parse_entry_time(entry)

        # RSS 通常按时间倒序排列，一旦发现过期直接跳出
        if pub_date and pub_date < cutoff_time:
            break

        entries.append(
            {
                "title": _truncate_title(entry.get("title", "无标题")),
                "link": entry.get("link", ""),
                "published": pub_date,
                "source": feed_info["title"],
                "content": _extract_body(entry),
                "tags": [],
                "score": 0,
                "summary": "",
            }
        )

    return entries


async def _fetch_nitter_content(url: str, timeout: int) -> Optional[bytes]:
    """nitter / xcancel 专用：requests + Inoreader UA，丢线程池避免阻塞 loop"""

    def _sync():
        try:
            r = requests.get(url, headers=NITTER_HEADERS, timeout=timeout)
            if r.status_code != 200:
                print(f"⚠️ HTTP {r.status_code}: {url}")
                return None
            return r.content
        except Exception as e:
            print(f"⚠️ nitter 抓取失败 {url}: {e}")
            return None

    return await asyncio.to_thread(_sync)


async def _fetch_aiohttp_content(
    url: str, timeout: int, session: aiohttp.ClientSession = None
) -> Optional[str]:
    """普通 RSS 源：aiohttp + 浏览器 UA"""
    client_timeout = aiohttp.ClientTimeout(total=timeout)

    if session is not None:
        async with session.get(
            url, headers=DEFAULT_HEADERS, timeout=client_timeout
        ) as resp:
            if resp.status != 200:
                print(f"⚠️ HTTP {resp.status}: {url}")
                return None
            return await resp.text()

    async with aiohttp.ClientSession(trust_env=True) as sess:
        async with sess.get(
            url, headers=DEFAULT_HEADERS, timeout=client_timeout
        ) as resp:
            if resp.status != 200:
                print(f"⚠️ HTTP {resp.status}: {url}")
                return None
            return await resp.text()


async def fetch_single_feed_async(
    feed_info: Dict,
    cutoff_time: datetime,
    timeout: int = 5,
    session: aiohttp.ClientSession = None,
) -> List[Dict]:
    """异步获取单个源的条目"""
    try:
        if timeout is None:
            timeout = DEFAULT_FEED_TIMEOUT

        url = feed_info["xmlUrl"]

        if is_nitter_url(url):
            content = await _fetch_nitter_content(url, timeout)
        else:
            content = await _fetch_aiohttp_content(url, timeout, session)

        if content is None:
            return []

        return _parse_feed_entries(content, feed_info, cutoff_time)
    except Exception as e:
        err_msg = str(e) or type(e).__name__
        print(f"⚠️ 获取失败 {feed_info['title']}: {err_msg}")
        return []


async def fetch_all_feeds(
    feeds: List[Dict], cutoff_time: datetime, max_workers: int = 10, timeout: int = None
) -> List[Dict]:
    """并发获取所有源的条目；nitter/xcancel 走独立的低并发池"""
    if timeout is None:
        timeout = DEFAULT_FEED_TIMEOUT

    nitter_feeds = [f for f in feeds if is_nitter_url(f.get("xmlUrl", ""))]
    normal_feeds = [f for f in feeds if not is_nitter_url(f.get("xmlUrl", ""))]

    normal_sem = asyncio.Semaphore(max_workers)
    nitter_sem = asyncio.Semaphore(NITTER_MAX_CONCURRENCY)

    async def fetch_normal(feed):
        async with normal_sem:
            return await fetch_single_feed_async(feed, cutoff_time, timeout)

    async def fetch_nitter(feed):
        async with nitter_sem:
            result = await fetch_single_feed_async(feed, cutoff_time, timeout)
            # 公益实例：抓完 sleep，把同一 worker 串内的请求拉开
            await asyncio.sleep(NITTER_REQUEST_DELAY)
            return result

    ordered_feeds = normal_feeds + nitter_feeds
    tasks = [fetch_normal(f) for f in normal_feeds] + [
        fetch_nitter(f) for f in nitter_feeds
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_entries = []
    for feed, result in zip(ordered_feeds, results):
        if isinstance(result, Exception):
            err_msg = str(result) or type(result).__name__
            print(f"⚠️ 获取失败 {feed['title']}: {err_msg}")
        else:
            all_entries.extend(result)

    return all_entries
