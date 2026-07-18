"""测试新增 LLM 函数 (summarize_github_trending 等)"""

import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from llm import summarize_github_trending, select_ai_related_hn, summarize_hackernews


@pytest.mark.asyncio
async def test_summarize_github_trending_happy_path(tmp_path):
    prompt_path = tmp_path / "section_github.md"
    prompt_path.write_text("Repos: {repos_json}\nmax_items={max_items}", encoding="utf-8")

    config = {
        "model": "x",
        "baseUrl": "http://x",
        "apiKeyName": "DEEPSEEK_API_KEY",
        "prompts": {"section_github": str(prompt_path)},
        "sections": {"github_trending": {"max_items": 3}},
    }
    enriched = [{"full_name": "o/r", "readme_excerpt": "rm"}]

    with patch("llm.call_llm", new=AsyncMock(return_value="## md")):
        md, err = await summarize_github_trending(enriched, config)

    assert md == "## md"
    assert err is None


@pytest.mark.asyncio
async def test_summarize_github_trending_llm_failure_returns_error(tmp_path):
    prompt_path = tmp_path / "section_github.md"
    prompt_path.write_text("x {repos_json} {max_items}", encoding="utf-8")
    config = {
        "model": "x",
        "baseUrl": "http://x",
        "apiKeyName": "DEEPSEEK_API_KEY",
        "prompts": {"section_github": str(prompt_path)},
        "sections": {"github_trending": {"max_items": 3}},
    }
    with patch("llm.call_llm", new=AsyncMock(side_effect=RuntimeError("boom"))):
        md, err = await summarize_github_trending([{"full_name": "o/r"}], config)
    assert md == ""
    assert "boom" in err


@pytest.mark.asyncio
async def test_select_ai_related_hn_parses_id_array(tmp_path):
    prompt_path = tmp_path / "select.md"
    prompt_path.write_text("k={k} candidates={candidates_json}", encoding="utf-8")
    config = {
        "model": "x",
        "baseUrl": "http://x",
        "apiKeyName": "DEEPSEEK_API_KEY",
        "prompts": {"section_hackernews_select": str(prompt_path)},
    }
    with patch("llm.call_llm", new=AsyncMock(return_value='["111", "222"]')):
        ids, err = await select_ai_related_hn(
            [{"id": "111"}, {"id": "222"}, {"id": "333"}], k=2, config=config
        )
    assert ids == ["111", "222"]
    assert err is None


@pytest.mark.asyncio
async def test_select_ai_related_hn_empty_array(tmp_path):
    prompt_path = tmp_path / "select.md"
    prompt_path.write_text("{k}{candidates_json}", encoding="utf-8")
    config = {
        "model": "x",
        "baseUrl": "http://x",
        "apiKeyName": "DEEPSEEK_API_KEY",
        "prompts": {"section_hackernews_select": str(prompt_path)},
    }
    with patch("llm.call_llm", new=AsyncMock(return_value="[]")):
        ids, err = await select_ai_related_hn([{"id": "1"}], k=1, config=config)
    assert ids == []
    assert err is None


@pytest.mark.asyncio
async def test_summarize_hackernews_happy(tmp_path):
    prompt_path = tmp_path / "hn.md"
    prompt_path.write_text("{stories_json}", encoding="utf-8")
    config = {
        "model": "x",
        "baseUrl": "http://x",
        "apiKeyName": "DEEPSEEK_API_KEY",
        "prompts": {"section_hackernews": str(prompt_path)},
    }
    with patch("llm.call_llm", new=AsyncMock(return_value="## HN summary")):
        md, err = await summarize_hackernews(
            [{"id": "1", "title": "t", "link_content": "x", "top_comments": []}], config
        )
    assert md == "## HN summary"
    assert err is None
