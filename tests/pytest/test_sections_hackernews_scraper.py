"""测试 HN 首页 HTML 解析"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.sections.hackernews.frontpage_scraper import parse_frontpage_html


def test_parse_frontpage_returns_stories():
    fixture = (Path(__file__).parent / "fixtures" / "hn_frontpage.html").read_text(
        encoding="utf-8"
    )

    stories = parse_frontpage_html(fixture)
    assert len(stories) >= 25
    s = stories[0]
    print(json.dumps(stories[:5], indent=4, ensure_ascii=False))

    assert s["id"]
    assert s["title"]
    assert s["url"]
    assert isinstance(s["points"], int)
    assert isinstance(s["comments"], int)
    assert s["comments_url"].startswith("https://news.ycombinator.com/item?id=")


def test_parse_frontpage_detects_show_hn_internal_url():
    html = """
    <table>
      <tr class="athing" id="111">
        <td class="title">
          <span class="titleline">
            <a href="item?id=111">Ask HN: what's new?</a>
          </span>
        </td>
      </tr>
      <tr>
        <td class="subtext">
          <span class="subline">
            <span class="score">50 points</span>
            by <a href="user?id=alice">alice</a>
            <span class="age"><a href="item?id=111">2 hours ago</a></span>
            | <a href="item?id=111">5&nbsp;comments</a>
          </span>
        </td>
      </tr>
    </table>
    """
    stories = parse_frontpage_html(html)
    assert len(stories) == 1
    s = stories[0]
    assert s["id"] == "111"
    assert s["url"].startswith("https://news.ycombinator.com/item?id=")
    assert s["site"] == ""
    assert s["points"] == 50
    assert s["comments"] == 5
