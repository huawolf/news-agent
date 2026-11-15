"""LLM模块测试"""

import json
import pytest
import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from llm import (
    _split_entries_for_batch,
    _build_batch_prompt,
    _score_single_batch,
    call_llm,
)


class TestSplitEntriesForBatch:
    """测试条目分批"""

    def test_rendered_prompt_never_exceeds_the_budget(self):
        """The split must respect the budget on the *rendered* prompt.

        The prompt template carries the recent-titles block, so its length is
        far larger than the entries alone; estimating entries with a compact
        JSON silently overfilled every batch.
        """
        duplicate_titles = [
            {"id": i, "title": "历史高分标题" * 3, "first_seen_at": "2026-11-01T00:00:00Z"}
            for i in range(100)
        ]
        entries = [
            {
                "link": f"https://example.com/{i}",
                "title": f"标题{i}",
                "source": "某源",
                "content": "正文内容" * 50,
            }
            for i in range(30)
        ]

        base_prompt_chars = len(
            _build_batch_prompt(
                [],
                duplicate_titles=duplicate_titles,
                short_references=True,
            )
        )
        budget = base_prompt_chars + 8000
        batches = _split_entries_for_batch(
            entries, budget, duplicate_titles=duplicate_titles
        )

        assert len(batches) > 1
        for batch in batches:
            rendered = _build_batch_prompt(
                batch,
                duplicate_titles=duplicate_titles,
                short_references=True,
            )
            assert len(rendered) <= budget

class TestBuildBatchPrompt:
    """测试构建批量提示词"""

class TestMergeScores:
    """测试评分合并"""

class TestCallLlm:
    """测试LLM调用"""

    @pytest.mark.asyncio
    async def test_call_llm_missing_key(self):
        config = {"model": "gpt-4", "apiKeyName": "MISSING_KEY"}

        with pytest.raises(ValueError, match="未设置MISSING_KEY"):
            await call_llm("Test prompt", config)


class TestScoreBatch:
    """测试批量评分"""

    @pytest.mark.asyncio
    async def test_score_single_batch_reconcile_partial_results(
        self, sample_entries, sample_config
    ):
        entries = sample_entries[:2]
        llm_results = [
            {
                "link": entries[0]["link"],
                "score": 91,
                "tags": ["AI"],
                "summary": "Matched result",
            }
        ]

        with patch("llm.call_llm", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = json.dumps(llm_results, ensure_ascii=False)
            results, errors = await _score_single_batch(entries, sample_config["llm"])

        assert len(results) == 1
        assert results[0]["score"] == 91
        assert errors == []

    @pytest.mark.asyncio
    async def test_score_single_batch_invalid_json_is_log_only(
        self, sample_entries, sample_config
    ):
        config = {**sample_config["llm"], "batch_retry_delay_seconds": 0}
        with patch("llm.call_llm", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = "not json"
            results, errors = await _score_single_batch(
                sample_entries[:2], config
            )

        assert results == []
        assert errors == []

    @pytest.mark.asyncio
    async def test_score_single_batch_uses_short_reference_and_restores_link(
        self, sample_config
    ):
        entries = [
            {
                "link": "https://news.google.com/rss/articles/" + "x" * 500,
                "title": "Google News item",
                "source": "Google News",
                "content": "Content",
            }
        ]
        llm_results = [
            {
                "link": "item-1",
                "score": 90,
                "summary": "Matched result",
            }
        ]

        with patch("llm.call_llm", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = json.dumps({"items": llm_results}, ensure_ascii=False)
            results, errors = await _score_single_batch(entries, sample_config["llm"])

        prompt = mock_call.await_args.args[0]
        assert "item-1" in prompt
        assert entries[0]["link"] not in prompt
        assert results[0]["link"] == entries[0]["link"]
        assert results[0]["score"] == 90
        assert errors == []

    def test_build_batch_prompt_truncates_content_to_1000_chars(self):
        from src.llm import _build_batch_prompt

        long_content = "A" * 3000
        entries = [{"link": "https://example.com", "title": "Test", "source": "src", "content": long_content}]
        prompt = _build_batch_prompt(entries)

        # The entries block is the one right after the "## Input data" heading;
        # the prompt also carries a recent-titles block further down.
        entries_block = prompt.split("## Input data", 1)[1].split("```json\n", 1)[1].split("\n```", 1)[0]
        parsed_entries = json.loads(entries_block)
        assert len(parsed_entries[0]["content"]) == 1000

class TestLowContent:
    """The model must drop empty input, not describe how empty it is."""

    def test_a_long_body_is_never_low_content(self):
        """A caveat in a solid analysis is research judgment, not an excuse.

        ``MiniMax一站式AI动效工作流实测`` has a real body and merely notes it
        lacks commercial data. That must survive.
        """
        from llm import is_low_content_entry

        body = "MiniMax Design 实测正文。" * 40
        summary = "展示了单智能体包办宣发视频的潜力，但缺乏可验证的商业数据与成本信息。"
        assert not is_low_content_entry(summary, body)

    async def test_low_value_flag_is_merged_through(self, sample_entries, sample_config):
        """The scorer flags a readable-but-useless entry and the flag survives."""
        from llm import score_batch

        entries = sample_entries[:1]
        llm_results = [
            {
                "link": entries[0]["link"],
                "title": "某沙龙举办",
                "summary": "论坛于上周举办，多位嘉宾到场。",
                "score": 55,
                "quality_score": 55,
                "interest_score": 50,
                "assets": [],
                "sectors": [],
                "keywords": ["论坛", "沙龙"],
                "is_low_value": True,
            }
        ]

        with patch("llm.call_llm", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = json.dumps(llm_results, ensure_ascii=False)
            results, errors = await score_batch(entries, sample_config["llm"])

        assert errors == []
        assert results[0]["is_low_value"] is True

    async def test_meta_commentary_summary_is_cleared(self, sample_entries, sample_config):
        """A model that ignored the instruction must not leak commentary."""
        from llm import score_batch

        entries = sample_entries[:1]
        llm_results = [
            {
                "link": entries[0]["link"],
                "title": "10月30日晚间新闻完整版汇总",
                "summary": "该条目为节目聚合页，缺乏单一可验证事实与数据支撑。暂无直接关联资产与板块，仅作去重存档。",
                "score": 30,
                "quality_score": 30,
                "interest_score": 30,
                "assets": [],
                "sectors": [],
                "keywords": ["晚间新闻"],
            }
        ]

        with patch("llm.call_llm", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = json.dumps(llm_results, ensure_ascii=False)
            results, errors = await score_batch(entries, sample_config["llm"])

        assert errors == []
        assert results[0]["is_low_content"] is True
        assert results[0]["summary"] == ""
        assert results[0]["title"] == ""
