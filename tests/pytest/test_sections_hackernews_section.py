"""测试 HN 板块编排:scrape → select → enrich → LLM"""

import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Stub HN LLM functions until Task 16 lands them
import src.llm as _llm
if not hasattr(_llm, "select_ai_related_hn"):
    async def _stub_select(*a, **k):
        return [], None
    _llm.select_ai_related_hn = _stub_select
if not hasattr(_llm, "summarize_hackernews"):
    async def _stub_summarize(*a, **k):
        return "", None
    _llm.summarize_hackernews = _stub_summarize

from src.sections.hackernews.section import run_hackernews_section


def _cfg() -> dict:
    return {
        "filter": {"keep_days": 7},
        "sections": {
            "hackernews": {
                "enabled": True,
                "select_k": 1,
                "top_comments": 20,
                "comment_max_chars": 500,
                "link_content_max_chars": 3000,
                "request_timeout": 10,
                "algolia_base": "https://hn.algolia.com/api/v1",
            }
        },
        "llm": {
            "model": "x",
            "baseUrl": "http://x",
            "apiKeyName": "DEEPSEEK_API_KEY",
            "prompts": {
                "section_hackernews_select": "prompts/section_hackernews_select.md",
                "section_hackernews": "prompts/section_hackernews.md",
            },
        },
    }


@pytest.mark.asyncio
async def test_disabled_returns_empty():
    cfg = _cfg()
    cfg["sections"]["hackernews"]["enabled"] = False
    md, err = await run_hackernews_section(cfg, now=None)
    assert md == ""
    assert err is None


@pytest.mark.asyncio
async def test_select_empty_returns_silent():
    cfg = _cfg()
    with patch(
        "src.sections.hackernews.section.fetch_frontpage", new=AsyncMock(return_value="<html>")
    ), patch(
        "src.sections.hackernews.section.parse_frontpage_html",
        return_value=[{"id": "1", "title": "x"}],
    ), patch(
        "src.llm.select_ai_related_hn",
        new=AsyncMock(return_value=([], None)),
    ):
        md, err = await run_hackernews_section(cfg, now=None)
    assert md == ""
    assert err is None


@pytest.mark.asyncio
async def test_happy_path():
    cfg = _cfg()
    front = [{"id": "1", "title": "AI thing", "url": "https://e.com/a", "site": "e.com", "points": 100, "comments": 5, "comments_url": "x"}]
    enriched = [{**front[0], "link_content": "body", "top_comments": ["c1"]}]

    with patch(
        "src.sections.hackernews.section.fetch_frontpage", new=AsyncMock(return_value="<html>")
    ), patch(
        "src.sections.hackernews.section.parse_frontpage_html", return_value=front
    ), patch(
        "src.llm.select_ai_related_hn",
        new=AsyncMock(return_value=(["1"], None)),
    ), patch(
        "src.sections.hackernews.section.enrich_stories",
        new=AsyncMock(return_value=(enriched, [])),
    ), patch(
        "src.llm.summarize_hackernews",
        new=AsyncMock(return_value=("## HN md", None)),
    ):
        md, err = await run_hackernews_section(cfg, now=None)
    assert md == "## HN md"
    assert err is None


@pytest.mark.asyncio
async def test_scrape_failure_returns_error():
    cfg = _cfg()
    with patch(
        "src.sections.hackernews.section.fetch_frontpage",
        new=AsyncMock(side_effect=RuntimeError("net")),
    ):
        md, err = await run_hackernews_section(cfg, now=None)
    assert md == ""
    assert "net" in err
