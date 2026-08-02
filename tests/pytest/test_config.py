"""配置模块测试"""

import json
import pytest
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from config import (
    load_config,
    parse_opml,
    merge_sources,
    get_timezone,
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

    def test_load_missing_file(self):
        with pytest.raises(FileNotFoundError):
            load_config("nonexistent.json")

    def test_load_invalid_json(self, temp_dir):
        config_file = temp_dir / "config.json"
        config_file.write_text("invalid json")
        with pytest.raises(json.JSONDecodeError):
            load_config(str(config_file))

    def test_load_config_with_sources(self, temp_dir, sample_opml):
        config = {
            "sources": {
                "base_opml": sample_opml,
                "add": [
                    {
                        "title": "Add1",
                        "xmlUrl": "http://add1.com/rss",
                        "category": "test",
                    }
                ],
                "block": [],
            },
            "filter": {"min_score": 60},
            "schedule": {"fetch_interval_minutes": 30, "timezone_hours": 8},
            "llm": {"provider": "test"},
            "push": {"discord": {"enabled": False}},
        }
        config_file = temp_dir / "config.json"
        config_file.write_text(json.dumps(config))

        result = load_config(str(config_file))
        assert len(result["sources"]["add"]) == 1


class TestParseOpml:
    """测试OPML解析"""

    def test_parse_valid_opml(self, temp_dir):
        opml_content = """<?xml version="1.0"?>
<opml version="2.0">
    <body>
        <outline title="Feed1" xmlUrl="http://feed1.com/rss" type="rss"/>
    </body>
</opml>"""
        opml_file = temp_dir / "test.opml"
        opml_file.write_text(opml_content)

        feeds = parse_opml(str(opml_file))
        assert len(feeds) == 1
        assert feeds[0]["title"] == "Feed1"
        assert feeds[0]["xmlUrl"] == "http://feed1.com/rss"

    def test_parse_missing_file(self):
        feeds = parse_opml("nonexistent.opml")
        assert feeds == []

    def test_parse_opml_with_category(self, temp_dir):
        opml_content = """<?xml version="1.0"?>
<opml version="2.0">
    <body>
        <outline title="TechFeed" xmlUrl="http://tech.com/rss" type="rss" category="技术"/>
        <outline title="AIFeed" xmlUrl="http://ai.com/rss" type="rss" category="AI"/>
    </body>
</opml>"""
        opml_file = temp_dir / "test.opml"
        opml_file.write_text(opml_content)

        feeds = parse_opml(str(opml_file))
        assert len(feeds) == 2
        assert feeds[0]["category"] == "技术"
        assert feeds[1]["category"] == "AI"

    def test_parse_opml_empty_body(self, temp_dir):
        opml_content = """<?xml version="1.0"?>
<opml version="2.0">
    <body>
    </body>
</opml>"""
        opml_file = temp_dir / "test.opml"
        opml_file.write_text(opml_content)

        feeds = parse_opml(str(opml_file))
        assert feeds == []


class TestMergeSources:
    """测试源合并"""

    def test_merge_base_and_add(self, sample_opml):
        config = {
            "base_opml": sample_opml,
            "add": [
                {"title": "Feed3", "xmlUrl": "http://feed3.com/rss", "category": "test"}
            ],
            "block": [],
        }
        sources = merge_sources(config)
        assert len(sources) == 3

    def test_block_by_xmlUrl(self, sample_opml):
        config = {
            "base_opml": sample_opml,
            "add": [],
            "block": [{"title": "Block1", "xmlUrl": "http://feed1.com/rss"}],
        }
        sources = merge_sources(config)
        assert all(s["xmlUrl"] != "http://feed1.com/rss" for s in sources)
        assert len(sources) == 1

    def test_deduplicate_by_xmlUrl(self, sample_opml):
        config = {
            "base_opml": sample_opml,
            "add": [
                {
                    "title": "Duplicate",
                    "xmlUrl": "http://feed1.com/rss",
                    "category": "test",
                }
            ],
            "block": [],
        }
        sources = merge_sources(config)
        urls = [s["xmlUrl"] for s in sources]
        assert len(urls) == len(set(urls))
        assert len(sources) == 2

    def test_block_domains_wildcard(self, temp_dir):
        opml_content = """<?xml version="1.0"?>
<opml version="2.0">
    <body>
        <outline title="Substack" xmlUrl="https://tech.substack.com/rss" type="rss"/>
        <outline title="Blog" xmlUrl="https://tech.blog/rss" type="rss"/>
    </body>
</opml>"""
        opml_file = temp_dir / "test.opml"
        opml_file.write_text(opml_content)

        config = {
            "base_opml": str(opml_file),
            "add": [],
            "block": [],
            "block_domains": ["*.substack.com"],
        }
        sources = merge_sources(config)
        assert len(sources) == 1
        assert sources[0]["xmlUrl"] == "https://tech.blog/rss"

    def test_block_domains_exact(self, temp_dir):
        opml_content = """<?xml version="1.0"?>
<opml version="2.0">
    <body>
        <outline title="YouTube" xmlUrl="https://youtube.com/feed" type="rss"/>
        <outline title="Blog" xmlUrl="https://tech.blog/rss" type="rss"/>
    </body>
</opml>"""
        opml_file = temp_dir / "test.opml"
        opml_file.write_text(opml_content)

        config = {
            "base_opml": str(opml_file),
            "add": [],
            "block": [],
            "block_domains": ["youtube.com"],
        }
        sources = merge_sources(config)
        assert len(sources) == 1
        assert sources[0]["xmlUrl"] == "https://tech.blog/rss"

    def test_block_domains_subdomain(self, temp_dir):
        opml_content = """<?xml version="1.0"?>
<opml version="2.0">
    <body>
        <outline title="Substack1" xmlUrl="https://substack.com/feed" type="rss"/>
        <outline title="Substack2" xmlUrl="https://ai.substack.com/feed" type="rss"/>
        <outline title="Blog" xmlUrl="https://tech.blog/rss" type="rss"/>
    </body>
</opml>"""
        opml_file = temp_dir / "test.opml"
        opml_file.write_text(opml_content)

        config = {
            "base_opml": str(opml_file),
            "add": [],
            "block": [],
            "block_domains": ["*.substack.com"],
        }
        sources = merge_sources(config)
        assert len(sources) == 1
        assert sources[0]["xmlUrl"] == "https://tech.blog/rss"


class TestGetTimezone:
    """测试时区获取"""

    def test_get_timezone_from_config(self, sample_config):
        tz = get_timezone(sample_config)
        assert isinstance(tz, timezone)
        assert tz.utcoffset(datetime.now()).total_seconds() == 8 * 3600

    def test_get_timezone_none_config(self):
        tz = get_timezone(None)
        assert isinstance(tz, timezone)

    def test_get_timezone_no_timezone_hours(self):
        config = {"schedule": {}}
        tz = get_timezone(config)
        assert isinstance(tz, timezone)

    def test_get_timezone_custom_hours(self):
        config = {"schedule": {"timezone_hours": -5}}
        tz = get_timezone(config)
        assert tz.utcoffset(datetime.now()).total_seconds() == -5 * 3600
