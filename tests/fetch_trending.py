"""手动跑一次 GH trending 抓取 + deep-dive,验证选择器与 API 接入"""

import asyncio
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import load_config
from src.sections.github.repo_enricher import enrich_repos
from src.sections.github.trending_scraper import (
    fetch_trending_page,
    parse_trending_html,
)


async def main():
    config = load_config()
    print("📥 抓取 GitHub Trending...")
    html = await fetch_trending_page(timeout=15)
    repos = parse_trending_html(html)
    print(f"📋 解析出 {len(repos)} 个 repo")
    for r in repos[:5]:
        print(f"  - {r['full_name']} ⭐{r['stars_today']}/{r['stars_total']} | {r['description'][:80]}")

    cfg = config["sections"]["github_trending"]
    print(f"\n🔍 enrich 前 {min(3, len(repos))} 个...")
    enriched, errors = await enrich_repos(
        repos[:3],
        token_env=cfg.get("tokenName", "GITHUB_TOKEN"),
        readme_max_chars=cfg.get("readme_max_chars", 3000),
        timeout=15,
    )
    for e in errors:
        print(f"  ⚠️ {e}")
    print(json.dumps(enriched, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
