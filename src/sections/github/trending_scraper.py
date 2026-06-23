"""GitHub Trending 单页 HTML 抓取与解析。

数据源: https://github.com/trending (无 language / since 过滤)
"""

import re
from typing import Dict, List

import aiohttp
from bs4 import BeautifulSoup

TRENDING_URL = "https://github.com/trending"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_NUM_RE = re.compile(r"[\d,]+")


def _parse_int(s: str) -> int:
    m = _NUM_RE.search(s or "")
    if not m:
        return 0
    return int(m.group(0).replace(",", ""))


async def fetch_trending_page(timeout: int = 10) -> str:
    """抓取 trending 页 HTML;非 200 抛 RuntimeError"""
    async with aiohttp.ClientSession(
        headers={"User-Agent": USER_AGENT},
        trust_env=True
    ) as session:
        async with session.get(
            TRENDING_URL, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(
                    f"GitHub trending 返回 {resp.status}: {text[:200]}"
                )
            return await resp.text()


def parse_trending_html(html: str) -> List[Dict]:
    """解析 trending HTML,返回去重后的 repo 字典数组。"""
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    seen_urls = set()
    repos: List[Dict] = []

    for article in soup.select("article.Box-row"):
        h2 = article.find("h2")
        a = h2.find("a") if h2 else None
        if not a or not a.get("href"):
            continue

        href = a["href"].strip()
        full_name = href.lstrip("/")
        url = f"https://github.com/{full_name}"
        if url in seen_urls:
            continue
        seen_urls.add(url)

        # description
        desc_tag = article.find("p")
        description = (desc_tag.get_text(strip=True) if desc_tag else "") or ""

        # language
        lang_tag = article.find("span", attrs={"itemprop": "programmingLanguage"})
        language = (lang_tag.get_text(strip=True) if lang_tag else "") or ""

        # stars_total: 第一个指向 /stargazers 的链接
        stars_total = 0
        star_a = article.find("a", href=re.compile(r"/stargazers$"))
        if star_a:
            stars_total = _parse_int(star_a.get_text(strip=True))

        # stars_today: 末尾的 "N stars today" span
        stars_today = 0
        for span in article.find_all("span"):
            t = span.get_text(strip=True)
            if "stars today" in t or "stars this week" in t or "stars this month" in t:
                stars_today = _parse_int(t)
                break

        repos.append(
            {
                "url": url,
                "full_name": full_name,
                "description": description,
                "language": language,
                "stars_today": stars_today,
                "stars_total": stars_total,
            }
        )

    return repos
