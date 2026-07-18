#!/usr/bin/env python3
"""预览 LLM 查重上下文 —— 直接打印 load_recent_notify_content / load_recent_push_content
返回的内容,便于调试历史去重时实际喂给 LLM 的素材。

Usage:
    # 默认:读取 config.json 的 filter.context_days,打印 notify + push 两段
    python tests/preview_recent_context.py

    # 指定回溯天数
    python tests/preview_recent_context.py --days 5

    # 只看一种
    python tests/preview_recent_context.py --type notify
    python tests/preview_recent_context.py --type push

    # 自定义数据目录(例如 tests/news-data 里的样本数据)
    python tests/preview_recent_context.py --data-dir tests/news-data
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import load_config
from src.storage import load_recent_notify_content, load_recent_push_content


def _resolve_days(cli_days: int | None) -> int:
    if cli_days is not None:
        return cli_days
    try:
        config = load_config()
        return int(config.get("filter", {}).get("context_days", 3))
    except Exception:
        return 3


def _print_section(label: str, content: str) -> None:
    sep = "=" * 60
    print(f"\n{sep}\n{label}\n{sep}")
    if not content:
        print("(空)")
        return
    print(content)
    print(
        f"\n--- {label} 字符数: {len(content)} | 块数(按 `------` 切分): "
        f"{len([b for b in content.split('------') if b.strip()])} ---"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="预览近 N 天 notify / push 文件正文 (剥离 frontmatter 后)"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="回溯天数,默认读取 config.filter.context_days(兜底 3)",
    )
    parser.add_argument(
        "--type",
        choices=["notify", "push", "both"],
        default="both",
        help="预览哪种历史,默认 both",
    )
    parser.add_argument(
        "--data-dir",
        default="news-data",
        help="数据目录,默认 news-data",
    )
    parser.add_argument(
        "--section",
        default="rss",
        help="push 文件取哪个 sentinel 段,默认 rss(可选 github / hackernews / insights)",
    )
    args = parser.parse_args()

    days = _resolve_days(args.days)
    print(
        f"📅 回溯天数: {days}  |  📂 数据目录: {args.data_dir}  |  "
        f"类型: {args.type}  |  push.section: {args.section}"
    )

    if args.type in ("notify", "both"):
        notify_md = load_recent_notify_content(days, args.data_dir)
        _print_section("近期即时推送 (notify)", notify_md)

    if args.type in ("push", "both"):
        push_md = load_recent_push_content(days, args.data_dir, section=args.section)
        _print_section(f"近期汇总推送 (push.{args.section})", push_md)

    return 0


if __name__ == "__main__":
    sys.exit(main())
