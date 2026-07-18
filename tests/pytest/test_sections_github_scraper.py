"""测试 GitHub trending HTML 解析"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.sections.github.trending_scraper import parse_trending_html


def test_parse_trending_html_returns_repo_dicts():
    fixture = (
        Path(__file__).parent / "fixtures" / "github_trending.html"
    ).read_text(encoding="utf-8")

    repos = parse_trending_html(fixture)

    assert len(repos) > 0
    first = repos[0]
    assert first["url"].startswith("https://github.com/")
    assert "/" in first["full_name"]
    assert isinstance(first["stars_today"], int)
    assert isinstance(first["stars_total"], int)
    # description / language 可为空字符串但必须是 str
    assert isinstance(first["description"], str)
    assert isinstance(first["language"], str)


def test_parse_trending_html_dedupes_by_url():
    fixture = (
        Path(__file__).parent / "fixtures" / "github_trending.html"
    ).read_text(encoding="utf-8")
    repos = parse_trending_html(fixture)
    urls = [r["url"] for r in repos]
    assert len(urls) == len(set(urls))


def test_parse_trending_html_empty_input():
    assert parse_trending_html("") == []
    assert parse_trending_html("<html><body>no repos</body></html>") == []
