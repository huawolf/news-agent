"""手动跑一次 xcancel/nitter 抓取，验证白名单 UA + requests 路径

用法：python tests/fetch_nitter.py
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.fetcher import fetch_all_feeds

FEEDS = [
    {
        "title": "Alibaba_Qwen",
        "xmlUrl": "https://rss.xcancel.com/Alibaba_Qwen/rss",
    },
    {
        "title": "trq212",
        "xmlUrl": "https://rss.xcancel.com/trq212/rss",
    },
    {
        "title": "Elon Musk",
        "xmlUrl": "https://nitter.privacyredirect.com/elonmusk/rss",
    },
]


async def main():
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    print(f"📥 抓取 {len(FEEDS)} 个 nitter 源（cutoff={cutoff.isoformat()}）...")

    entries = await fetch_all_feeds(FEEDS, cutoff)

    print(f"📋 拿到 {len(entries)} 条")
    for e in entries[:10]:
        published = e["published"].isoformat() if e["published"] else "?"
        print(f"  - [{published}] {e['source']} {e['title']}")
        print(f"    {e['link']}")
        print(f"    {e['content']}\n\n")

    if not entries:
        print("⚠️ 一条都没拿到 —— 检查 UA / TLS 路径是否生效")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
