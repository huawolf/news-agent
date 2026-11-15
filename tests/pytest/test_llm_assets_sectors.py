"""评分契约测试：资产标准化、板块受控取值、direction 枚举。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from dedup.tokenizer import normalize_keywords  # noqa: E402
from llm import (  # noqa: E402
    _build_batch_prompt,
    _merge_scores,
    _output_language_instruction,
    normalize_assets,
)

PROJECT_ROOT = Path(__file__).parent.parent.parent


class TestSectorsPromptBlock:
    """板块注入提示词"""

    def test_batch_prompt_injects_sectors(self):
        prompt = _build_batch_prompt(
            [{"link": "https://example.com/a", "title": "t", "source": "s", "content": "c"}],
            prompt_path=str(PROJECT_ROOT / "prompts" / "score_batch.md"),
            sectors=[{"name": "加密货币", "keywords": ["比特币"]}],
        )
        assert "加密货币" in prompt
        assert "{sectors}" not in prompt
        assert "{entries_json}" not in prompt

class TestOutputLanguageScope:
    """输出语言只作用于自然语言内容，不影响 assets 与 sectors"""

    ENTRY = [{"link": "https://example.com/a", "title": "t", "source": "s", "content": "c"}]

    @staticmethod
    def _render(output_language: str) -> str:
        config = {"output_language": output_language}
        return _build_batch_prompt(
            TestOutputLanguageScope.ENTRY,
            prompt_path=str(PROJECT_ROOT / "prompts" / "score_batch.md"),
            output_language_instruction=_output_language_instruction(config, json_mode=True),
        )

    def test_zh_mode_still_forces_english_assets(self):
        prompt = self._render("zh")
        assert "Simplified Chinese" in prompt
        assert "always English in both Chinese and English output" in prompt
        assert "assets` is always English" in prompt

class TestNormalizeAssets:
    """资产写法归一化"""

    def test_non_ascii_dropped(self):
        """资产名只允许英文；中文一律丢弃。"""
        assert normalize_assets(["英伟达", "原油", "纳斯达克 100"]) == []

    def test_caps_at_five_assets(self):
        """每条信号最多 5 个资产 (§5.1)。"""
        raw = ["nvda", "aapl", "btc", "gold", "silver", "copper", "dxy"]
        assert normalize_assets(raw) == ["NVDA", "AAPL", "BTC", "GOLD", "SILVER"]


class TestMergeScores:
    """评分结果合并到原始条目"""

    @staticmethod
    def _entry():
        return {"link": "https://example.com/a", "title": "t", "source": "s"}

    def test_normalizes_and_filters(self):
        merged = _merge_scores(
            [self._entry()],
            [
                {
                    "link": "https://example.com/a",
                    "score": 90,
                    "quality_score": 88,
                    "interest_score": 92,
                    "assets": ["nvda", "$AAPL", "crude oil", "btc", "BTC", "原油"],
                    "direction": "bullish",
                    "sectors": ["AI技术", "不存在板块"],
                }
            ],
            sectors=[{"name": "AI技术"}],
        )
        assert merged[0]["assets"] == ["NVDA", "AAPL", "CRUDEOIL", "BTC"]
        assert merged[0]["sectors"] == ["AI技术"]
        assert merged[0]["direction"] == "bullish"

    def test_tags_field_is_not_reintroduced(self):
        """`tags` was removed: the LLM must not be able to inject it back in."""
        merged = _merge_scores(
            [self._entry()],
            [
                {
                    "link": "https://example.com/a",
                    "score": 90,
                    "tags": ["GPT-5", "多模态"],
                    "sectors": ["AI技术"],
                }
            ],
            sectors=[{"name": "AI技术"}],
        )
        assert "tags" not in merged[0]


class TestNormalizeKeywords:
    """`keywords` 固定 10 个，且不能被字符串拆成字符 (§5.2)"""

    ENTRY = {
        "link": "https://example.com/a",
        "title": "nvidia beats earnings estimates",
        "content": "Nvidia raised guidance for the next quarter.",
        "source": "s",
    }

    def test_bare_string_is_rejected_not_split(self):
        """`list("abc")` 会变成 a/b/c 三个关键词，污染去重与查重。"""
        assert normalize_keywords("nvidia") == []

    def test_keeps_ten_distinct_lowercase_keywords(self):
        raw = [f"kw{i}" for i in range(25)]
        result = normalize_keywords(raw)
        assert len(result) == 10
        assert result == [f"kw{i}" for i in range(10)]

