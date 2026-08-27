import asyncio
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


if __name__ == "__main__":
    unittest.main()
