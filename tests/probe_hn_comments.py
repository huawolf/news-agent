"""Probe HN: 抓取首页前 10 条,统计每个 story 评论树的深度/数量/字符数。

用于判断 enrich 策略:是否需要 L2 回复、压缩、过滤短评等。

字符数说明:
- "raw" = Algolia 返回的 text 字段(HTML)
- "md"  = html_to_markdown 后的字符数(LLM 实际看到的)
"""

import asyncio
import json
import os
import sys
from typing import Dict, List, Tuple

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.processor import html_to_markdown
from src.sections.hackernews.frontpage_scraper import (
    fetch_frontpage,
    parse_frontpage_html,
)

ALGOLIA = "https://hn.algolia.com/api/v1/items"
TIMEOUT = 30


async def fetch_item(session: aiohttp.ClientSession, item_id: str) -> Dict:
    async with session.get(
        f"{ALGOLIA}/{item_id}",
        timeout=aiohttp.ClientTimeout(total=TIMEOUT),
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"{item_id} -> {resp.status}")
        return await resp.json()


def walk_tree(node: Dict, depth: int = 0) -> List[Tuple[int, str]]:
    """递归遍历评论树,返回 [(depth, text_html), ...]。depth=0 是 story 自身,1 是顶层评论。"""
    result: List[Tuple[int, str]] = []
    text = (node or {}).get("text") or ""
    if depth > 0 and text:
        result.append((depth, text))
    for child in (node or {}).get("children") or []:
        result.extend(walk_tree(child, depth + 1))
    return result


def stats(samples: List[str]) -> Dict:
    if not samples:
        return {"count": 0, "raw_chars": 0, "md_chars": 0, "avg_md": 0, "max_md": 0}
    md_lens = [len(html_to_markdown(s)) for s in samples]
    raw_lens = [len(s) for s in samples]
    return {
        "count": len(samples),
        "raw_chars": sum(raw_lens),
        "md_chars": sum(md_lens),
        "avg_md": round(sum(md_lens) / len(md_lens)),
        "max_md": max(md_lens),
    }


def by_depth(nodes: List[Tuple[int, str]], d: int) -> List[str]:
    return [t for depth, t in nodes if depth == d]


async def probe_one(
    session: aiohttp.ClientSession, story: Dict
) -> Dict:
    item_id = story["id"]
    data = await fetch_item(session, item_id)
    flat = walk_tree(data)
    l1 = by_depth(flat, 1)
    l2 = by_depth(flat, 2)
    l3plus = [t for d, t in flat if d >= 3]
    all_comments = [t for _, t in flat]
    return {
        "id": item_id,
        "title": story["title"][:60],
        "page_comments": story["comments"],
        "L1": stats(l1),
        "L2": stats(l2),
        "L3+": stats(l3plus),
        "ALL": stats(all_comments),
    }


def print_row(label: str, s: Dict):
    print(
        f"  {label:5s} count={s['count']:4d}  md_total={s['md_chars']:7d}  "
        f"avg={s['avg_md']:5d}  max={s['max_md']:6d}"
    )


async def main():
    print("📥 抓取 HN 首页...")
    html = await fetch_frontpage(timeout=15)
    stories = parse_frontpage_html(html)[:10]
    print(f"📋 取前 10 条 (实际 {len(stories)} 条)\n")

    async with aiohttp.ClientSession(trust_env=True) as session:
        results = await asyncio.gather(
            *[probe_one(session, s) for s in stories],
            return_exceptions=True,
        )

    summary = {"L1": [], "L2": [], "L3+": [], "ALL": []}
    for r in results:
        if isinstance(r, Exception):
            print(f"❌ {r}\n")
            continue
        print(
            f"#{r['id']}  page_comments={r['page_comments']}\n  {r['title']}"
        )
        print_row("L1", r["L1"])
        print_row("L2", r["L2"])
        print_row("L3+", r["L3+"])
        print_row("ALL", r["ALL"])
        print()
        for key in summary:
            summary[key].append(r[key])

    print("=" * 70)
    print("📊 10 条 story 汇总(平均/总和)\n")
    for key in ["L1", "L2", "L3+", "ALL"]:
        rows = summary[key]
        total_count = sum(r["count"] for r in rows)
        total_md = sum(r["md_chars"] for r in rows)
        avg_count_per_story = round(total_count / len(rows), 1) if rows else 0
        avg_md_per_story = round(total_md / len(rows)) if rows else 0
        print(
            f"  {key:5s} 总数={total_count:4d}  总md字符={total_md:8d}  "
            f"平均每story count={avg_count_per_story:5.1f} md_chars={avg_md_per_story:6d}"
        )


if __name__ == "__main__":
    asyncio.run(main())
