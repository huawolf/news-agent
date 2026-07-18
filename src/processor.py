"""内容处理模块 - HTML转Markdown"""

import re
from urllib.parse import urljoin

from markdownify import markdownify as md


def html_to_markdown(html: str, base_url: str = "") -> str:
    """
    将HTML转换为Markdown，保留链接和图片
    使用markdownify库，并进行后处理优化
    """
    # markdownify会自动处理<img>为![](url)，<a>为[text](url)
    markdown = md(html, heading_style="ATX")

    # 处理相对链接
    if base_url:

        def replace_rel_link(m):
            prefix, path, suffix = m.groups()
            if path.startswith(("http://", "https://", "data:")):
                return m.group(0)
            abs_url = urljoin(base_url, path)
            return f"{prefix}{abs_url}{suffix}"

        markdown = re.sub(r"(!?\[.*?\]\()(.*?)(\))", replace_rel_link, markdown)

    # 后处理优化
    # 1. 直接匹配移除 xgo.ing 推广链接
    markdown = markdown.replace("[⚡ Powered by xgo.ing](https://xgo.ing)", "")
    markdown = markdown.replace("[⚡ Powered by xgo.ing](https://xgo.ing/)", "")

    # 2. 清理多余空行
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)

    return markdown.strip()


def is_daily_newsletter_entry(entry: dict) -> bool:
    """
    判断条目是否为聚合类的日报/周报等，通过元数据头部（如 title:, lead:, highlights:）或标题关键字识别。
    """
    title = entry.get("title", "") or ""
    content = entry.get("content", "") or ""

    title_lower = title.lower()
    content_lower = content.lower()

    # 1. 检查内容前部是否包含 title:, lead:, highlights: 元数据标记（支持中英文冒号）
    content_prefix = content_lower[:1000]
    has_title_meta = "title:" in content_prefix or "title：" in content_prefix
    has_lead_meta = "lead:" in content_prefix or "lead：" in content_prefix
    has_highlights_meta = "highlights:" in content_prefix or "highlights：" in content_prefix

    if has_title_meta and has_lead_meta and has_highlights_meta:
        return True

    # 2. 检查标题中是否含有明显的日报关键字
    daily_keywords = ["ai daily", "每日精选", "每日晚报", "每日快讯", "每日资讯", "每日早报", "日报"]
    for kw in daily_keywords:
        if kw in title_lower:
            return True

    return False

