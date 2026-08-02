"""测试 GitHub 板块编排:抓取 → history 过滤 → enrich → LLM 总结"""

import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.sections.github.section import run_github_section

# Stub summarize_github_trending until Task 11 provides it
import src.llm as _llm
if not hasattr(_llm, "summarize_github_trending"):
    async def _stub(*a, **k):
        return "", None
    _llm.summarize_github_trending = _stub


def _cfg(history_file: str, max_deep_dive: int = 10) -> dict:
    return {
        "filter": {"keep_days": 7},
        "sections": {
            "github_trending": {
                "enabled": True,
                "max_items": 3,
                "max_deep_dive": max_deep_dive,
                "readme_max_chars": 3000,
                "history_file": history_file,
                "request_timeout": 10,
                "tokenName": "GITHUB_TOKEN",
            }
        },
        "llm": {
            "model": "x",
            "baseUrl": "http://x",
            "apiKeyName": "DEEPSEEK_API_KEY",
            "prompts": {"section_github": "prompts/section_github.md"},
        },
    }


@pytest.mark.asyncio
async def test_disabled_returns_empty(tmp_path):
    cfg = _cfg(str(tmp_path / "h.json"))
    cfg["sections"]["github_trending"]["enabled"] = False
    md, err = await run_github_section(cfg, now=None)
    assert md == ""
    assert err is None


@pytest.mark.asyncio
async def test_no_candidates_after_history_returns_empty(tmp_path):
    history_path = tmp_path / "h.json"
    # 预置 history,使得今日 scrape 出来的 repo 都已存在
    history_path.write_text(
        '{"repos": {"https://github.com/a/b": "2026-05-16"}, "updated_at": "x"}',
        encoding="utf-8",
    )
    cfg = _cfg(str(history_path))

    with patch(
        "src.sections.github.section.fetch_trending_page", new=AsyncMock(return_value="<html>")
    ), patch(
        "src.sections.github.section.parse_trending_html",
        return_value=[{"url": "https://github.com/a/b", "full_name": "a/b"}],
    ):
        md, err = await run_github_section(cfg, now=None)

    assert md == ""
    assert err is None


@pytest.mark.asyncio
async def test_happy_path_enriches_and_summarizes(tmp_path):
    history_path = tmp_path / "h.json"
    cfg = _cfg(str(history_path), max_deep_dive=10)

    repos = [
        {
            "url": "https://github.com/o1/r1",
            "full_name": "o1/r1",
            "description": "d1",
            "language": "Python",
            "stars_today": 100,
            "stars_total": 1000,
        }
    ]
    enriched = [{**repos[0], "topics": ["llm"], "license": "MIT", "pushed_at": "p", "readme_excerpt": "rm"}]

    with patch(
        "src.sections.github.section.fetch_trending_page", new=AsyncMock(return_value="<html>")
    ), patch(
        "src.sections.github.section.parse_trending_html", return_value=repos
    ), patch(
        "src.sections.github.section.enrich_repos",
        new=AsyncMock(return_value=(enriched, [])),
    ), patch(
        "src.llm.summarize_github_trending",
        new=AsyncMock(return_value=("## GH section md", None)),
    ):
        md, err = await run_github_section(cfg, now=None)

    assert md == "## GH section md"
    assert err is None
    import json as _j
    saved = _j.loads(history_path.read_text(encoding="utf-8"))
    assert "https://github.com/o1/r1" in saved["repos"]


@pytest.mark.asyncio
async def test_truncates_candidates_to_max_deep_dive(tmp_path):
    cfg = _cfg(str(tmp_path / "h.json"), max_deep_dive=2)
    repos = [
        {"url": f"https://github.com/o/r{i}", "full_name": f"o/r{i}"} for i in range(5)
    ]
    captured = {}

    async def fake_enrich(candidates, **kwargs):
        captured["count"] = len(candidates)
        return [], []

    with patch(
        "src.sections.github.section.fetch_trending_page", new=AsyncMock(return_value="<html>")
    ), patch(
        "src.sections.github.section.parse_trending_html", return_value=repos
    ), patch(
        "src.sections.github.section.enrich_repos", new=AsyncMock(side_effect=fake_enrich)
    ):
        await run_github_section(cfg, now=None)

    assert captured["count"] == 2


@pytest.mark.asyncio
async def test_scrape_failure_returns_error(tmp_path):
    cfg = _cfg(str(tmp_path / "h.json"))
    with patch(
        "src.sections.github.section.fetch_trending_page",
        new=AsyncMock(side_effect=RuntimeError("HTTP 500")),
    ):
        md, err = await run_github_section(cfg, now=None)
    assert md == ""
    assert "HTTP 500" in err
