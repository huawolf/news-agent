#!/usr/bin/env python3
"""重新推送已保存的 push 文件

Usage:
    python tests/resend_push.py news-data/push-2026-05-21-08-00-00.md
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


async def resend_push_file(filepath: str, config: dict):
    """读取 push 文件，解析 metadata 并重新推送

    Args:
        filepath: push 文件路径
        config: 配置字典

    Returns:
        bool: 推送是否成功
    """
    if not Path(filepath).exists():
        print(f"❌ 文件不存在: {filepath}")
        return False

    # 读取文件内容
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # 解析 frontmatter（复用 markdown_utils）
    metadata, body = parse_frontmatter(content)
    if not metadata:
        print("❌ 文件格式错误：无法解析 YAML frontmatter")
        return False

    # 拼接推送标题，与 main.py 的 push 任务保持一致
    raw_title = metadata.get("title", "")
    title = "📰 AI Daily 每日精选 | " + raw_title if raw_title else "📰 AI Daily 每日精选"

    print(f"\n📤 准备推送文件: {Path(filepath).name}")
    print(f"   标题: {title}")
    print(f"   Profile: {metadata.get('profile', 'N/A')}")
    print(f"   日期: {metadata.get('date') or metadata.get('pushDate', 'N/A')}")

    # 推送到所有平台
    try:
        await send_to_platforms(body, config["push"], title=title, metadata=metadata)
        print("\n✅ 推送成功!")
        return True
    except Exception as e:
        print(f"\n❌ 推送失败: {e}")
        import traceback

        traceback.print_exc()
        return False


async def main():
    parser = argparse.ArgumentParser(description="重新推送已保存的 push 文件")
    parser.add_argument("filepath", help="push 文件路径")
    args = parser.parse_args()

    print("=" * 60)
    print("📤 重新推送工具")
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
        print(f"   {status} {platform_name}: enabled={enabled}, has_key={has_key} ({api_key_name})")

    # 推送文件
    success = await resend_push_file(args.filepath, config)
    return 0 if success else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n\n👋 已取消")
        sys.exit(130)
