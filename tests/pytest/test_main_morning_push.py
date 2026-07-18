"""测试早报四模块编排:gather + insights 串行 + sentinel 拼装 + 失败隔离"""

import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.main import _run_morning_push


def _insights_meta() -> dict:
    return {
        "title": "📰 AI Daily 每日精选 | 2026-05-22",
        "lead": "lead text",
        "highlights": ["a"],
        "profile": "morning",
        "date": "2026-05-22",
        "excerpt": "",
        "seotitle": "",
        "seodescription": "",
    }


@pytest.mark.asyncio
async def test_assembles_all_four_sections(sample_config):
    sample_config["filter"]["push_context_days"] = 5

    sent = {}

    async def fake_send(content, push_cfg, title=None, metadata=None):
        sent["content"] = content
        sent["metadata"] = metadata

    saved = {}

    def fake_save(
        filepath,
        content,
        source_count,
        total_entries,
        profile="default",
        metadata=None,
    ):
        saved["profile"] = profile
        saved["content"] = content
        saved["metadata"] = metadata

    with patch(
        "src.main.run_rss_section", new=AsyncMock(return_value=("R", None, None))
    ), patch(
        "src.main.run_github_section", new=AsyncMock(return_value=("G", None))
    ), patch(
        "src.main.run_hackernews_section", new=AsyncMock(return_value=("H", None))
    ), patch(
        "src.main.run_insights_section",
        new=AsyncMock(return_value=("I", _insights_meta(), None)),
    ), patch(
        "src.main.send_to_platforms", new=AsyncMock(side_effect=fake_send)
    ), patch(
        "src.main.save_push_file", side_effect=fake_save
    ):
        await _run_morning_push(sample_config)

    assert "SECTION:rss" in sent["content"]
    assert "SECTION:github" in sent["content"]
    assert "SECTION:hackernews" in sent["content"]
    assert "SECTION:insights" in sent["content"]
    assert sent["metadata"]["profile"] == "morning"
    assert saved["profile"] == "morning"
    assert saved["metadata"]["lead"] == "lead text"


@pytest.mark.asyncio
async def test_rss_failure_raises_to_caller(sample_config):
    sample_config["filter"]["push_context_days"] = 5

    with patch(
        "src.main.run_rss_section",
        new=AsyncMock(return_value=("", None, "compose_digest 失败")),
    ), patch(
        "src.main.run_github_section", new=AsyncMock(return_value=("G", None))
    ), patch(
        "src.main.run_hackernews_section", new=AsyncMock(return_value=("H", None))
    ), patch(
        "src.main.notify_llm_errors", new=AsyncMock()
    ):
        with pytest.raises(RuntimeError):
            await _run_morning_push(sample_config)


@pytest.mark.asyncio
async def test_section_failure_degrades_to_omission(sample_config):
    sample_config["filter"]["push_context_days"] = 5

    sent = {}

    async def fake_send(content, push_cfg, title=None, metadata=None):
        sent["content"] = content

    with patch(
        "src.main.run_rss_section", new=AsyncMock(return_value=("R", None, None))
    ), patch(
        "src.main.run_github_section", new=AsyncMock(return_value=("", "gh down"))
    ), patch(
        "src.main.run_hackernews_section", new=AsyncMock(return_value=("H", None))
    ), patch(
        "src.main.run_insights_section",
        new=AsyncMock(return_value=("I", _insights_meta(), None)),
    ), patch(
        "src.main.notify_llm_errors", new=AsyncMock()
    ), patch(
        "src.main.send_to_platforms", new=AsyncMock(side_effect=fake_send)
    ), patch(
        "src.main.save_push_file"
    ):
        await _run_morning_push(sample_config)

    assert "SECTION:rss" in sent["content"]
    assert "SECTION:github" not in sent["content"]
    assert "SECTION:hackernews" in sent["content"]
