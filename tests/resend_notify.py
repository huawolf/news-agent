#!/usr/bin/env python3
"""重新推送即时消息（notify 文件）

Usage:
    python tests/resend_notify.py news-data/notify-2026-05-21.md
    python tests/resend_notify.py news-data/notify-2026-05-21.md --index 0  # 推送第一个块
"""

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import load_config
from src.markdown_utils import parse_frontmatter
from src.push import send_to_platforms


def parse_notify_file(filepath: str):
    """解析 notify 文件，返回所有推送块列表

    Returns:
        List[Dict]: [{"metadata": {...}, "content": "..."}, ...]
    """
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    blocks = []
    for block in content.split("------"):
        block = block.strip()
        if not block:
            continue

        metadata, body = parse_frontmatter(block)
        if not metadata:
            continue

        blocks.append({"metadata": metadata, "content": body})

    return blocks


async def resend_notify(filepath: str, config: dict, index: int = -1):
    """重新推送 notify 文件中的即时消息

    Args:
        filepath: notify 文件路径
        config: 配置字典
        index: 推送第几个块（-1 表示最新的一个）

    Returns:
        bool: 推送是否成功
    """
    if not Path(filepath).exists():
        print(f"❌ 文件不存在: {filepath}")
        return False

    blocks = parse_notify_file(filepath)
    if not blocks:
        print("❌ 文件中没有有效的推送块")
        return False

    print(f"\n📋 文件中共有 {len(blocks)} 个推送块")

    # 选择要推送的块
    if index == -1:
        block = blocks[-1]
        print(f"   使用最新的一个（第 {len(blocks)} 个）")
    elif 0 <= index < len(blocks):
        block = blocks[index]
        print(f"   使用第 {index + 1} 个")
    else:
        print(f"❌ 索引超出范围: {index} (有效范围: 0-{len(blocks)-1})")
        return False

    metadata = block["metadata"]
    content = block["content"]
    # 拼接推送标题，与 main.py 的即时推送保持一致
    raw_title = metadata.get("title", "")
    title = "🚨 AI Daily 快讯 | " + raw_title if raw_title else "🚨 AI Daily 快讯"

    print(f"\n📤 准备推送:")
    print(f"   标题: {title}")
    print(f"   时间: {metadata.get('pushTime', 'N/A')}")

    # 推送
    try:
        await send_to_platforms(content, config["push"], title=title, metadata=metadata)
        print("\n✅ 推送成功!")
        return True
    except Exception as e:
        print(f"\n❌ 推送失败: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    parser = argparse.ArgumentParser(description="重新推送即时消息（notify 文件）")
    parser.add_argument("filepath", help="notify 文件路径")
    parser.add_argument("--index", type=int, default=-1, help="推送第几个块（-1=最新，默认）")
    args = parser.parse_args()

    print("=" * 60)
    print("🚨 即时消息推送工具")
    print("=" * 60)

    # 加载配置
    try:
        config = load_config()
        print("✅ 配置加载成功")
    except Exception as e:
        print(f"❌ 加载配置失败: {e}")
        return 1

    # 显示推送平台配置状态
    print("\n📋 推送平台配置:")
    import os
    for platform_name, platform_conf in config.get("push", {}).items():
        enabled = platform_conf.get("enabled", False)
        api_key_name = platform_conf.get("apiKeyName", "")
        has_key = bool(os.environ.get(api_key_name, ""))
        status = "✅" if (enabled and has_key) else "⚠️"
        print(f"   {status} {platform_name}: enabled={enabled}, has_key={has_key}")

    # 推送
    success = await resend_notify(args.filepath, config, args.index)
    return 0 if success else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n\n👋 已取消")
        sys.exit(130)
