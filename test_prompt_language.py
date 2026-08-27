import asyncio
import ast
import re
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.llm import _output_language_instruction, translate_markdown_section


PROJECT_ROOT = Path(__file__).resolve().parent
PROMPT_FILES = (
    "prompts/digest.md",
    "prompts/immediate_push.md",
    "prompts/insights.md",
    "prompts/score_batch.md",
    "prompts/section_github.md",
    "prompts/section_hackernews.md",
    "prompts/section_hackernews_select.md",
    "prompts/translate_section.md",
)
CJK_TEXT = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
RUNTIME_MESSAGE_FILES = (
    "src/fetcher.py",
    "src/llm.py",
    "src/storage.py",
    "src/push/__init__.py",
    "src/push/custom.py",
    "src/push/discord.py",
    "src/push/feishu.py",
    "src/sections/rss/section.py",
    "src/sections/hackernews/section.py",
    "src/sections/hackernews/item_enricher.py",
    "src/sections/hackernews/frontpage_scraper.py",
    "src/sections/github/section.py",
    "src/sections/github/repo_enricher.py",
    "src/sections/github/trending_scraper.py",
    "src/config.py",
)


class PromptLanguageRegressionTests(unittest.TestCase):
    def test_english_instruction_is_self_contained_and_authoritative(self):
        instruction = _output_language_instruction({"output_language": "en"})

        self.assertIn("Output language: English", instruction)
        self.assertIn("source material is in another language", instruction)
        self.assertIn("Translate source-language prose into English", instruction)
        self.assertNotRegex(instruction, CJK_TEXT)

    def test_prompt_templates_do_not_embed_a_chinese_output_language(self):
        for relative_path in PROMPT_FILES:
            with self.subTest(prompt=relative_path):
                prompt = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
                self.assertNotRegex(prompt, CJK_TEXT)

    def test_english_translation_retries_and_rejects_mixed_output(self):
        first_draft = "### 1. English heading\n中文残留"
        second_draft = "### 1. English heading\nEnglish paragraph"
        llm = AsyncMock(side_effect=[first_draft, second_draft])

        with patch("src.llm.call_llm", llm):
            translated, error = asyncio.run(
                translate_markdown_section(
                    first_draft, {"output_language": "en"}, "GitHub Trending"
                )
            )

        self.assertIsNone(error)
        self.assertEqual(translated, second_draft)
        self.assertEqual(llm.await_count, 2)

    def test_runtime_messages_are_english(self):
        """Operational logs and raised errors must not leak untranslated CJK text."""
        for relative_path in RUNTIME_MESSAGE_FILES:
            source = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative_path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    segment = ast.get_source_segment(source, node) or ""
                    function_name = ""
                    if isinstance(node.func, ast.Name):
                        function_name = node.func.id
                    elif isinstance(node.func, ast.Attribute):
                        function_name = node.func.attr
                    if function_name not in {
                        "append",
                        "error",
                        "exception",
                        "info",
                        "print",
                        "warning",
                        "_llm_error_message",
                    }:
                        continue
                    if function_name == "append" and "errors.append" not in segment:
                        continue
                elif isinstance(node, ast.Raise):
                    pass
                else:
                    continue

                with self.subTest(file=relative_path, line=node.lineno):
                    self.assertNotRegex(segment, CJK_TEXT)

    def test_batch_mismatch_message_is_english(self):
        from src.llm import _reconcile_batch_results

        entries = [{"link": "https://example.com/item"}]
        matched, errors = _reconcile_batch_results(entries, [], batch_index=10)

        self.assertEqual(matched, [])
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
