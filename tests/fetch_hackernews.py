"""手动跑一次 HN 首页 + Algolia enrich,验证选择器与 API"""

import asyncio
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.sections.hackernews.frontpage_scraper import (
    fetch_frontpage,
    parse_frontpage_html,
)
from src.sections.hackernews.item_enricher import enrich_stories


async def main():
    print("📥 抓取 HN 首页...")
    html = await fetch_frontpage(timeout=15)
    stories = parse_frontpage_html(html)
    print(f"📋 解析出 {len(stories)} 条")
    for s in stories[:5]:
        print(f"  - [{s['points']} pts · {s['comments']} comments] {s['title']} ({s['site']})")

    print("\n🔍 enrich 前 1 个外链类故事...")
    target = next((s for s in stories if not s["url"].startswith("https://news.ycombinator.com/")), stories[0])
    enriched, errors = await enrich_stories(
        [target],
        top_comments=5,
        top_l2_per_l1=3,
        comment_max_chars=2000,
        comments_total_chars=60000,
        link_content_max_chars=1500,
        timeout=15,
    )
    for e in errors:
        print(f"  ⚠️ {e}")
    print(json.dumps(enriched, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
