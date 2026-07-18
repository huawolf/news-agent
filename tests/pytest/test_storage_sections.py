"""测试新增的 sentinel 切片与 section-aware 读取"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from storage import extract_section


class TestExtractSection:
    def test_extract_section_with_sentinel(self):
        md = (
            "intro\n"
            "<!-- SECTION:rss BEGIN -->\n"
            "RSS body\n"
            "<!-- SECTION:rss END -->\n"
            "\n"
            "<!-- SECTION:github BEGIN -->\n"
            "GH body\n"
            "<!-- SECTION:github END -->\n"
        )
        assert extract_section(md, "rss").strip() == "RSS body"
        assert extract_section(md, "github").strip() == "GH body"
        assert extract_section(md, "hackernews") == ""

    def test_extract_section_legacy_file_rss(self):
        legacy = "# AI Daily\n### 1️⃣ foo\n### 2️⃣ bar\n"
        assert extract_section(legacy, "rss") == legacy

    def test_extract_section_legacy_file_non_rss(self):
        legacy = "# AI Daily\n### 1️⃣ foo\n"
        assert extract_section(legacy, "github") == ""
        assert extract_section(legacy, "hackernews") == ""
        assert extract_section(legacy, "insights") == ""

    def test_extract_section_missing_end_marker(self):
        broken = "<!-- SECTION:rss BEGIN -->\ncontent only\n"
        assert extract_section(broken, "rss") == ""


from datetime import date, datetime, timedelta
from storage import save_push_file


from storage import TrendingHistory, load_trending_history


class TestTrendingHistory:
    def test_load_missing_file(self, tmp_path):
        path = tmp_path / "trending.json"
        h = load_trending_history(str(path))
        assert h.repos == {}

    def test_touch_then_save_then_reload(self, tmp_path):
        path = tmp_path / "trending.json"
        h = load_trending_history(str(path))
        today = date(2026, 5, 17)
        h.touch("https://github.com/a/b", today)
        h.touch("https://github.com/c/d", today)
        h.save()

        h2 = load_trending_history(str(path))
        assert h2.repos == {
            "https://github.com/a/b": "2026-05-17",
            "https://github.com/c/d": "2026-05-17",
        }

    def test_contains_returns_membership(self, tmp_path):
        h = load_trending_history(str(tmp_path / "x.json"))
        h.touch("https://github.com/a/b", date(2026, 5, 17))
        assert "https://github.com/a/b" in h
        assert "https://github.com/x/y" not in h

    def test_cleanup_removes_expired_entries(self, tmp_path):
        path = tmp_path / "trending.json"
        path.write_text(
            '{"repos": {'
            '"https://github.com/old/repo": "2026-05-01", '
            '"https://github.com/new/repo": "2026-05-15"'
            '}, "updated_at": "2026-05-15T00:00:00+08:00"}',
            encoding="utf-8",
        )
        h = load_trending_history(str(path))
        h.cleanup(today=date(2026, 5, 17), keep_days=7)
        assert "https://github.com/old/repo" not in h
        assert "https://github.com/new/repo" in h

    def test_cleanup_keeps_today_inclusive(self, tmp_path):
        h = load_trending_history(str(tmp_path / "x.json"))
        h.touch("https://github.com/a/b", date(2026, 5, 10))
        # 2026-05-10 + 7 days = 2026-05-17 (last_seen 2026-05-10 仍在 keep 区间)
        h.cleanup(today=date(2026, 5, 17), keep_days=7)
        assert "https://github.com/a/b" in h
        # 再过 1 天就出区间
        h.cleanup(today=date(2026, 5, 18), keep_days=7)
        assert "https://github.com/a/b" not in h


class TestSavePushFileProfile:
    def test_default_profile_when_not_specified(self, tmp_path):
        f = tmp_path / "push-x.md"
        save_push_file(str(f), "body content", source_count=1, total_entries=1)
        text = f.read_text(encoding="utf-8")
        assert 'profile: "default"' in text
        assert "body content" in text

    def test_morning_profile(self, tmp_path):
        f = tmp_path / "push-x.md"
        save_push_file(
            str(f), "body", source_count=2, total_entries=3, profile="morning"
        )
        text = f.read_text(encoding="utf-8")
        assert 'profile: "morning"' in text


from storage import cleanup_old_files
import json as _j


class TestCleanupOldFilesTrendingHistory:
    def test_prunes_trending_history_entries_not_file(self, tmp_path):
        path = tmp_path / "trending-history.json"
        old_date = (datetime.now().date() - timedelta(days=30)).isoformat()
        fresh_date = datetime.now().date().isoformat()
        path.write_text(
            '{"repos": {'
            f'"https://github.com/a/b": "{old_date}", '
            f'"https://github.com/c/d": "{fresh_date}"'
            '}, "updated_at": "..."}',
            encoding="utf-8",
        )
        cleanup_old_files(days=7, data_dir=str(tmp_path))
        # 文件应保留
        assert path.exists()
        # 过期条目应被剪枝
        data = _j.loads(path.read_text(encoding="utf-8"))
        assert "https://github.com/a/b" not in data["repos"]
        assert "https://github.com/c/d" in data["repos"]


from storage import assemble_with_sentinels


class TestAssembleWithSentinels:
    def test_assembles_all_sections_in_order(self):
        out = assemble_with_sentinels(
            {"rss": "R", "github": "G", "hackernews": "H", "insights": "I"}
        )
        assert out.index("SECTION:rss") < out.index("SECTION:github")
        assert out.index("SECTION:github") < out.index("SECTION:hackernews")
        assert out.index("SECTION:hackernews") < out.index("SECTION:insights")
        assert "<!-- SECTION:rss BEGIN -->\nR\n<!-- SECTION:rss END -->" in out

    def test_omits_empty_sections(self):
        out = assemble_with_sentinels({"rss": "R", "github": "", "hackernews": "H", "insights": ""})
        assert "SECTION:github" not in out
        assert "SECTION:insights" not in out
        assert "SECTION:rss" in out
        assert "SECTION:hackernews" in out

    def test_returns_empty_when_all_empty(self):
        assert assemble_with_sentinels({"rss": "", "github": "", "hackernews": "", "insights": ""}) == ""
