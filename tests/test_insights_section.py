"""测试 insights section - 从早报文件解析三个板块并生成洞察"""

import asyncio
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

from src.config import get_timezone, load_config
from src.sections.insights.section import run_insights_section

load_dotenv()


def parse_sections(content: str) -> dict:
    """从早报内容中解析出各个板块"""
    sections = {}
    pattern = r"<!-- SECTION:(\w+) BEGIN -->\n(.*?)\n<!-- SECTION:\1 END -->"

    for match in re.finditer(pattern, content, re.DOTALL):
        section_name = match.group(1)
        section_content = match.group(2)
        sections[section_name] = section_content

    return sections


async def main():
    if len(sys.argv) < 2:
        print("用法: python test_insights_section.py <早报文件路径>")
        print(
            "示例: python test_insights_section.py news-data/push-2026-05-25-08-00-00.md"
        )
        sys.exit(1)

    filepath = sys.argv[1]

    # 读取早报文件
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # 解析板块
    sections = parse_sections(content)

    print(f"📄 解析文件: {filepath}")
    print(f"📋 找到板块: {list(sections.keys())}")
    print()

    rss_md = sections.get("rss", "")
    gh_md = sections.get("github", "")
    hn_md = sections.get("hackernews", "")

    if not rss_md and not gh_md and not hn_md:
        print("❌ 未找到任何板块内容")
        sys.exit(1)

    print(f"RSS 板块: {len(rss_md)} 字符")
    print(f"GitHub 板块: {len(gh_md)} 字符")
    print(f"HackerNews 板块: {len(hn_md)} 字符")
    print()

    # 加载配置
    config = load_config()
    now = datetime.now(get_timezone(config))

    # 调用 insights section
    print("🤖 生成洞察中...")
    insights_md, metadata, error = await run_insights_section(
        rss_md, gh_md, hn_md, config, now
    )

    if error:
        print(f"❌ 错误: {error}")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("📊 Insights 板块结果:")
    print("=" * 60)
    print(insights_md)
    print("=" * 60)

    if metadata:
        print("\n📋 Metadata:")
        for key, value in metadata.items():
            print(f"  {key}: {value}")


if __name__ == "__main__":
    asyncio.run(main())
