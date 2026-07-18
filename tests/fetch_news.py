#!/usr/bin/env python3
"""获取新闻脚本 - 模块化测试第一步：获取RSS并存储"""

import argparse
import asyncio
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

from src.config import get_timezone, load_config, merge_sources
from src.fetcher import fetch_all_feeds
from src.processor import html_to_markdown
from src.storage import append_entries, get_fetch_file, load_existing_links


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description="RSS新闻获取测试")
    parser.add_argument(
        "--hours", "-H", type=int, default=1, help="获取过去多少小时的新闻 (默认: 1)"
    )
    parser.add_argument("--minutes", "-m", type=int, help="获取过去多少分钟的新闻")
    parser.add_argument(
        "--output-dir",
        "-o",
        type=str,
        default="tests/news-data",
        help="输出目录 (默认: tests/news-data)",
    )
    parser.add_argument(
        "--max-per-domain",
        type=int,
        default=30,
        help="同一域名最大保留源数量 (默认: 30)",
    )
    return parser.parse_args()


def get_cutoff_time(args) -> datetime:
    """根据参数计算截止时间 (返回 UTC 时间)"""
    now = datetime.now(timezone.utc)
    if args.minutes:
        return now - timedelta(minutes=args.minutes)
    return now - timedelta(hours=args.hours)


def limit_sources_by_domain(sources: list, max_per_domain: int = 30) -> list:
    """限制同一域名的源数量"""
    domain_sources = defaultdict(list)

    for source in sources:
        url = source.get("xmlUrl", "")
        try:
            domain = urlparse(url).netloc.lower()
            # 移除 www 前缀
            if domain.startswith("www."):
                domain = domain[4:]
        except Exception:
            domain = "unknown"
        domain_sources[domain].append(source)

    limited_sources = []
    domain_counts = {}

    for domain, src_list in domain_sources.items():
        kept = src_list[:max_per_domain]
        limited_sources.extend(kept)
        domain_counts[domain] = {"total": len(src_list), "kept": len(kept)}

    return limited_sources, domain_counts


async def fetch_news():
    """主函数：获取RSS新闻并存储"""
    args = parse_args()
    tz = get_timezone()

    print("=" * 60)
    print("📰 RSS新闻获取测试 (Step 1)")
    print("=" * 60)

    # 1. 加载配置
    print("\n📋 加载配置...")
    config = load_config()
    all_sources = merge_sources(config["sources"])
    print(f"   OPML 解析完成: {len(all_sources)} 个源")

    # 2. 域名限制
    print(f"\n🔍 域名限制 (每域名最多 {args.max_per_domain} 个)...")
    sources, domain_stats = limit_sources_by_domain(all_sources, args.max_per_domain)

    # 打印域名统计
    total_domains = len(domain_stats)
    limited_domains = sum(
        1 for d in domain_stats.values() if d["total"] > args.max_per_domain
    )

    print(f"   域名总数: {total_domains}")
    print(f"   受限域名: {limited_domains}")
    print(f"   最终保留: {len(sources)} 个源")

    # 显示受限域名详情
    for domain, stats in sorted(domain_stats.items(), key=lambda x: -x[1]["total"])[:5]:
        if stats["total"] > args.max_per_domain:
            print(f"   - {domain}: {stats['total']} → {stats['kept']}")

    # 3. 计算时间窗口
    cutoff = get_cutoff_time(args)
    print(f"\n⏰ 时间窗口")
    print(f"   UTC: {cutoff.strftime('%Y-%m-%d %H:%M')}")
    print(
        f"   Local: {(datetime.now(tz) - (datetime.now(timezone.utc) - cutoff)).strftime('%Y-%m-%d %H:%M')}"
    )

    # 4. 获取RSS数据
    print(f"\n📡 开始获取...")
    max_workers = config.get("fetch", {}).get("max_workers", 10)
    timeout = config.get("fetch", {}).get("timeout", 5)
    entries = await fetch_all_feeds(
        sources, cutoff, max_workers=max_workers, timeout=timeout
    )

    # 5. 统计结果
    print(f"\n📊 获取统计")
    print(f"   读取源数: {len(all_sources)}")
    print(f"   保留源数: {len(sources)}")
    print(f"   获取条目: {len(entries)}")

    if not entries:
        print("\n⚠️ 没有获取到新消息")
        return 0

    # 6. 转换HTML到Markdown
    print("\n📝 处理内容...")
    for entry in entries:
        entry["content"] = html_to_markdown(
            entry.get("content", ""), entry.get("link", "")
        )

    # 7. 保存到文件
    print(f"\n💾 保存到文件...")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 使用今天的日期作为文件名（JSON格式）
    today = datetime.now(tz).date()
    fetch_file = output_dir / f"fetch-{today.isoformat()}.json"

    # 加载已有链接去重
    existing_links = (
        load_existing_links(str(fetch_file)) if fetch_file.exists() else set()
    )
    new_entries = [
        e for e in entries if e.get("link") and e["link"] not in existing_links
    ]

    # 添加时间戳并格式化
    for entry in entries:
        entry["fetched_at"] = datetime.now(tz).isoformat()
        if isinstance(entry.get("published"), datetime):
            entry["published"] = entry["published"].astimezone(tz).isoformat()

    # 使用新的 append_entries 批量保存
    meta = {"date": today.isoformat()}
    append_entries(str(fetch_file), entries, meta)

    print(f"   文件: {fetch_file}")
    print(f"   保存: {len(entries)} 条")
    print(f"   新增: {len(new_entries)} 条")
    print(f"   重复: {len(entries) - len(new_entries)} 条")

    print("\n" + "=" * 60)
    print("✅ Step 1 完成: RSS获取并存储")
    print("=" * 60)

    return len(entries)


if __name__ == "__main__":
    try:
        count = asyncio.run(fetch_news())
        sys.exit(0 if count > 0 else 1)
    except KeyboardInterrupt:
        print("\n\n👋 已取消")
        sys.exit(130)
    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
