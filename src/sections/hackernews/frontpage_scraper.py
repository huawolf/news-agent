"""HN 首页 HTML 抓取与解析。

数据源: https://news.ycombinator.com/news (30 条)
"""

import re
from typing import Dict, List

import aiohttp
from bs4 import BeautifulSoup

FRONTPAGE_URL = "https://news.ycombinator.com/news"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_NUM_RE = re.compile(r"\d+")


def _first_int(text: str) -> int:
    m = _NUM_RE.search(text or "")
    return int(m.group(0)) if m else 0


async def fetch_frontpage(timeout: int = 10) -> str:
    async with aiohttp.ClientSession(
        headers={"User-Agent": USER_AGENT}, trust_env=True
    ) as session:
        async with session.get(
            FRONTPAGE_URL, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HN frontpage 返回 {resp.status}")
            return await resp.text()


def parse_frontpage_html(html: str) -> List[Dict]:
    """解析首页 HTML,返回 [{id, title, url, site, points, comments, comments_url}]

    注:HN frontpage 的 athing 行的实际 class 是 'athing submission' (多类),
    用 CSS 选择器 'tr.athing' 仍然匹配。
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    stories: List[Dict] = []

    for athing in soup.select("tr.athing"):
        item_id = athing.get("id")
        if not item_id:
            continue

        title_a = athing.select_one("span.titleline > a")
        if not title_a:
            continue
        title = title_a.get_text(strip=True)
        href = title_a.get("href", "")
        if href.startswith("item?id="):
            url = f"https://news.ycombinator.com/{href}"
            site = ""
        else:
            url = href
            site_tag = athing.select_one("span.sitestr")
            site = site_tag.get_text(strip=True) if site_tag else ""

        sub_tr = athing.find_next_sibling("tr")
        points = 0
        comments = 0
        comments_url = f"https://news.ycombinator.com/item?id={item_id}"
        if sub_tr:
            score = sub_tr.select_one("span.score")
            if score:
                points = _first_int(score.get_text(strip=True))
            comment_a = None
            for a in sub_tr.find_all("a", href=re.compile(r"^item\?id=")):
                comment_a = a
            if comment_a:
                comments = _first_int(comment_a.get_text(strip=True))
                comments_url = f"https://news.ycombinator.com/{comment_a['href']}"

        stories.append(
            {
                "id": item_id,
                "title": title,
                "url": url,
                "site": site,
                "points": points,
                "comments": comments,
                "comments_url": comments_url,
            }
        )

    return stories
