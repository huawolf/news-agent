"""内容处理模块测试"""

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from processor import html_to_markdown


class TestHtmlToMarkdown:
    """测试HTML转Markdown"""

    def test_convert_basic_html(self):
        html = "<p>Hello World</p>"
        result = html_to_markdown(html)
        assert "Hello World" in result

    def test_convert_with_links(self):
        html = '<a href="https://example.com">Click here</a>'
        result = html_to_markdown(html)
        assert "[Click here](https://example.com)" in result

    def test_convert_with_images(self):
        html = '<img src="https://example.com/image.png" alt="Image">'
        result = html_to_markdown(html)
        assert "![Image](https://example.com/image.png)" in result

    def test_convert_with_headings(self):
        html = "<h1>Title</h1><h2>Subtitle</h2>"
        result = html_to_markdown(html)
        assert "# Title" in result
        assert "## Subtitle" in result

    def test_convert_with_lists(self):
        html = "<ul><li>Item 1</li><li>Item 2</li></ul>"
        result = html_to_markdown(html)
        assert "Item 1" in result
        assert "Item 2" in result

    def test_convert_with_strong_emphasis(self):
        html = "<strong>Bold</strong> and <em>italic</em>"
        result = html_to_markdown(html)
        assert "**Bold**" in result
        assert "*italic*" in result

    def test_relative_link_conversion(self):
        html = '<a href="/article/123">Read more</a>'
        result = html_to_markdown(html, base_url="https://example.com/blog")
        assert "https://example.com/article/123" in result

    def test_relative_image_conversion(self):
        html = '<img src="/images/logo.png">'
        result = html_to_markdown(html, base_url="https://example.com")
        assert "https://example.com/images/logo.png" in result

    def test_absolute_link_unchanged(self):
        html = '<a href="https://other.com/page">Link</a>'
        result = html_to_markdown(html, base_url="https://example.com")
        assert "https://other.com/page" in result

    def test_remove_xgo_ing_link(self):
        html = "<p>Content</p><p>[⚡ Powered by xgo.ing](https://xgo.ing)</p>"
        result = html_to_markdown(html)
        assert "xgo.ing" not in result
        assert "Content" in result

    def test_remove_xgo_ing_link_with_slash(self):
        html = "<p>Content</p><p>[⚡ Powered by xgo.ing](https://xgo.ing/)</p>"
        result = html_to_markdown(html)
        assert "xgo.ing" not in result

    def test_clean_extra_newlines(self):
        html = "<p>Line 1</p>\n\n\n\n<p>Line 2</p>"
        result = html_to_markdown(html)
        assert "\n\n\n\n" not in result

    def test_empty_html(self):
        result = html_to_markdown("")
        assert result.strip() == ""

    def test_html_with_nbsp(self):
        html = "<p>Hello&nbsp;World</p>"
        result = html_to_markdown(html)
        assert "Hello" in result
        assert "World" in result
