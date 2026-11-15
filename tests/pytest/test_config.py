"""配置模块测试"""

import json
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from config import (
    load_config,
    redact_database_url,
)


class TestLoadConfig:
    """测试配置加载"""

    def test_load_valid_config(self, temp_dir):
        config = {
            "sources": {"base_opml": "test.opml", "add": [], "block": []},
            "filter": {"min_score": 60},
            "schedule": {"fetch_interval_minutes": 30},
            "llm": {"provider": "groq", "model": "moonshotai/kimi-k2-instruct"},
            "push": {"discord": {"enabled": False}},
        }
        config_file = temp_dir / "config.json"
        config_file.write_text(json.dumps(config))

        result = load_config(str(config_file))
        assert result["filter"]["min_score"] == 60
        assert result["llm"]["provider"] == "groq"
        assert result["mode"] == "slaver"

    def test_load_missing_file(self):
        with pytest.raises(FileNotFoundError):
            load_config("nonexistent.json")

    def test_load_invalid_json(self, temp_dir):
        config_file = temp_dir / "config.json"
        config_file.write_text("invalid json")
        with pytest.raises(json.JSONDecodeError):
            load_config(str(config_file))


def test_database_url_redaction_keeps_credentials_out_of_output():
    sql = "postgresql+psycopg://news:secret@localhost:5432/news"

    assert redact_database_url(sql) == (
        "postgresql+psycopg://news:***@localhost:5432/news"
    )
    assert redact_database_url("sqlite:////tmp/news.db") == "sqlite:////tmp/news.db"

