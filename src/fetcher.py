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
    "nitter.privacyredirect.com",
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


async def fetch_hackernews_entries(config: Dict) -> List[Dict]:
    """获取 Hacker News 首页热门条目并富化内容 (无需 Jina Reader 抓取外链)"""
    try:
        from src.sections.hackernews.frontpage_scraper import fetch_frontpage, parse_frontpage_html
        from src.sections.hackernews.item_enricher import _fetch_algolia_item, _collect_comments_tree
        from src.processor import html_to_markdown
    except ImportError as e:
        print(f"⚠️ HN fetcher 依赖加载失败: {e}")
        return []

    cfg = config.get("sections", {}).get("hackernews", {})
    max_items = cfg.get("max_fetch_items", 30)
    algolia_base = cfg.get("algolia_base", "https://hn.algolia.com/api/v1")
    top_comments = cfg.get("top_comments", 50)
    top_l2_per_l1 = cfg.get("top_l2_per_l1", 3)
    comment_max_chars = cfg.get("comment_max_chars", 2000)
    comments_total_chars = cfg.get("comments_total_chars", 80000)
    timeout = cfg.get("request_timeout", 10)

    print("📥 HN: 抓取首页...")
    try:
        html = await fetch_frontpage(timeout=timeout)
        stories = parse_frontpage_html(html)[:max_items]
    except Exception as e:
        print(f"⚠️ HN frontpage 抓取失败: {e}")
        return []

    if not stories:
        return []

    print(f"📋 HN: 并发获取 {len(stories)} 条 story 的 Algolia 详细内容与热门评论...")
    async with aiohttp.ClientSession(
        headers={"User-Agent": "Mozilla/5.0"}, trust_env=True
    ) as session:
        tasks = []
        for s in stories:
            tasks.append(_fetch_algolia_item(session, s["id"], algolia_base, timeout))
        
        results = await asyncio.gather(*tasks, return_exceptions=True)

    entries = []
    for s, res in zip(stories, results):
        if isinstance(res, Exception) or not res:
            # Algolia 失败，兜底使用 frontpage 数据
            pub_date = datetime.now(timezone.utc)
            content_parts = [
                f"Points: {s.get('points', 0)} | Comments: {s.get('comments', 0)}"
            ]
            if s.get("site"):
                content_parts.insert(0, f"Domain: {s['site']}")
            content = "\n".join(content_parts)
        else:
            # 提取创建时间作为发布时间
            created_at = res.get("created_at")
            if created_at:
                try:
                    pub_date = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                except ValueError:
                    pub_date = datetime.now(timezone.utc)
            else:
                pub_date = datetime.now(timezone.utc)

            # 提取评论树
            comments_tree = _collect_comments_tree(
                res,
                top_comments=top_comments,
                top_l2_per_l1=top_l2_per_l1,
                comment_max_chars=comment_max_chars,
                comments_total_chars=comments_total_chars,
            )

            # 拼装内容
            content_parts = []
            if s.get("site"):
                content_parts.append(f"Domain: {s['site']}")
            content_parts.append(f"Points: {s.get('points', 0)} | Comments: {s.get('comments', 0)}")
            
            post_text = res.get("text")
            if post_text:
                content_parts.append("")
                content_parts.append("## Post Text")
                content_parts.append(html_to_markdown(post_text))

            if comments_tree:
                content_parts.append("")
                content_parts.append("[Top Comments]")
                for c in comments_tree:
                    l1_text = c.get("l1", "").strip()
                    if l1_text:
                        content_parts.append(f"- {l1_text}")
                        for r in c.get("replies", []):
                            r_text = r.strip()
                            if r_text:
                                content_parts.append(f"  - {r_text}")

            content = "\n".join(content_parts)

        entries.append({
            "title": s["title"],
            "link": s["url"],  # 统一使用源链接 (如果是 Ask/Show HN，则是 HN 帖子链接)
            "published": pub_date,
            "source": "Hacker News",
            "content": content,
            "tags": [],
            "score": 0,
            "summary": "",
        })

    return entries

