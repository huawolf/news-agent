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
