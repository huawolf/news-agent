"""存储模块测试"""

import json
import pytest
import sys
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from storage import (
    get_fetch_file,
    get_push_file,
    get_last_push_file,
    extract_push_time,
    read_entries,
    read_fetch_data,
    save_fetch_file,
    append_entries,
    format_entry,
    json_to_md,
    save_push_file,
    load_existing_links,
    cleanup_old_files,
    load_sent_links,
    mark_links_as_sent,
)


class TestGetFetchFile:
    """测试获取fetch文件路径"""

    def test_get_fetch_file_default(self):
        result = get_fetch_file()
        assert "fetch-" in result
        assert result.endswith(".json")

    def test_get_fetch_file_specific_date(self):
        result = get_fetch_file(date(2024, 1, 15))
        assert "fetch-2024-01-15.json" in result


class TestGetPushFile:
    """测试获取push文件路径"""

    def test_get_push_file_default(self):
        result = get_push_file()
        assert "push-" in result
        assert result.endswith(".md")

    def test_get_push_file_specific_time(self):
        dt = datetime(2024, 1, 15, 8, 30, 0)
        result = get_push_file(dt)
        assert "push-2024-01-15-08-30-00.md" in result


class TestExtractPushTime:
    """测试从文件名提取时间"""

    def test_extract_valid_time(self):
        result = extract_push_time("news-data/push-2024-01-15-08-30-00.md")
        assert result is not None
        assert result.year == 2024
        assert result.month == 1
        assert result.day == 15

    def test_extract_invalid_filename(self):
        result = extract_push_time("invalid.md")
        assert result is None


class TestGetLastPushFile:
    """测试获取最新push文件"""

    def test_get_last_push_file_empty(self, temp_dir):
        result = get_last_push_file(str(temp_dir))
        assert result is None

    def test_get_last_push_file_exists(self, temp_dir):
        (temp_dir / "push-2024-01-14-10-00-00.md").touch()
        (temp_dir / "push-2024-01-15-10-00-00.md").touch()

        result = get_last_push_file(str(temp_dir))
        assert "2024-01-15" in result


class TestReadWriteEntries:
    """测试读写条目"""

    def test_read_entries(self, sample_fetch_json):
        entries = read_entries(sample_fetch_json)
        assert len(entries) == 3
        assert entries[0]["title"] == "Article 1"

    def test_read_entries_missing_file(self):
        entries = read_entries("nonexistent.json")
        assert entries == []

    def test_read_fetch_data(self, sample_fetch_json):
        data = read_fetch_data(sample_fetch_json)
        assert "meta" in data
        assert "entries" in data
        assert len(data["entries"]) == 3


class TestSaveFetchFile:
    """测试保存fetch文件"""

    def test_save_fetch_file(self, temp_dir):
        filepath = str(temp_dir / "test.json")
        meta = {"date": "2024-01-15"}
        entries = [{"title": "Test", "link": "https://example.com", "score": 80}]

        save_fetch_file(filepath, meta, entries)

        data = read_fetch_data(filepath)
        assert data["meta"]["date"] == "2024-01-15"
        assert len(data["entries"]) == 1


class TestAppendEntries:
    """测试追加条目"""

    def test_append_new_entries(self, temp_dir):
        filepath = str(temp_dir / "test.json")
        meta = {"date": "2024-01-15"}

        entries1 = [{"title": "Entry1", "link": "https://example.com/1", "score": 80}]
        count1 = append_entries(filepath, entries1, meta)
        assert count1 == 1

        entries2 = [{"title": "Entry2", "link": "https://example.com/2", "score": 70}]
        count2 = append_entries(filepath, entries2, meta)
        assert count2 == 1

    def test_append_duplicate_entries(self, temp_dir):
        filepath = str(temp_dir / "test.json")
        meta = {"date": "2024-01-15"}

        entries = [{"title": "Entry1", "link": "https://example.com/1", "score": 80}]
        append_entries(filepath, entries, meta)

        all_entries = read_entries(filepath)
        assert len(all_entries) == 1

        count = append_entries(filepath, entries, meta)
        all_entries_after = read_entries(filepath)
        assert len(all_entries_after) == 1

    def test_append_to_existing_file(self, temp_dir):
        filepath = str(temp_dir / "test.json")

        data = {
            "meta": {"date": "2024-01-15"},
            "entries": [{"title": "Old", "link": "https://old.com", "score": 60}],
        }
        with open(filepath, "w") as f:
            json.dump(data, f)

        new_entries = [{"title": "New", "link": "https://new.com", "score": 70}]
        count = append_entries(filepath, new_entries)

        entries = read_entries(filepath)
        assert len(entries) == 2


class TestFormatEntry:
    """测试格式化条目"""

    def test_format_entry_basic(self, sample_entry):
        result = format_entry(sample_entry)
        assert "## Test Article Title" in result
        assert "source: Test Source" in result
        assert "score: 85" in result

    def test_format_entry_with_tags(self, sample_entry):
        result = format_entry(sample_entry)
        assert "AI" in result
        assert "Tech" in result


class TestJsonToMd:
    """测试JSON转Markdown"""

    def test_json_to_md_basic(self, sample_fetch_json):
        data = read_fetch_data(sample_fetch_json)
        result = json_to_md(data)

        assert "Article 1" in result
        assert "Article 2" in result
        assert "Article 3" in result

    def test_json_to_md_empty(self):
        data = {"meta": {}, "entries": []}
        result = json_to_md(data)
        assert result == ""


class TestSavePushFile:
    """测试保存推送文件"""

    def test_save_push_file(self, temp_dir):
        filepath = str(temp_dir / "push-test.md")
        content = "# Test Push\n\nContent here"

        save_push_file(filepath, content, 5, 10)

        with open(filepath, "r") as f:
            content = f.read()

        assert "pushDate:" in content
        assert "sourceCount: 5" in content
        assert "totalEntries: 10" in content
        assert "# Test Push" in content


class TestLoadExistingLinks:
    """测试加载已有链接"""

    def test_load_existing_links_json(self, sample_fetch_json):
        links = load_existing_links(sample_fetch_json)
        assert len(links) == 3
        assert "https://example.com/1" in links

    def test_load_existing_links_missing(self):
        links = load_existing_links("nonexistent.json")
        assert links == set()

    def test_load_existing_links_empty_string(self):
        links = load_existing_links("")
        assert links == set()


class TestCleanupOldFiles:
    """测试清理旧文件"""

    def test_cleanup_old_files(self, temp_dir):
        old_date = (datetime.now() - timedelta(days=10)).date()
        new_date = (datetime.now() - timedelta(days=1)).date()

        (temp_dir / f"fetch-{old_date}.json").touch()
        (temp_dir / f"fetch-{new_date}.json").touch()

        cleanup_old_files(days=7, data_dir=str(temp_dir))

        assert not (temp_dir / f"fetch-{old_date}.json").exists()
        assert (temp_dir / f"fetch-{new_date}.json").exists()

    def test_cleanup_push_files(self, temp_dir):
        old_time = datetime.now() - timedelta(days=10)
        new_time = datetime.now() - timedelta(days=1)

        (temp_dir / f"push-{old_time.strftime('%Y-%m-%d-%H-%M-%S')}.md").touch()
        (temp_dir / f"push-{new_time.strftime('%Y-%m-%d-%H-%M-%S')}.md").touch()

        cleanup_old_files(days=7, data_dir=str(temp_dir))

        files = list(temp_dir.glob("push-*.md"))
        assert len(files) == 1


class TestSentHistory:
    """测试已发送历史的加载与标记"""

    def test_sent_history_flow(self, temp_dir):
        data_dir = str(temp_dir)
        # 1. 初始应为空
        links = load_sent_links(days=3, data_dir=data_dir)
        assert links == set()

        # 2. 标记已发送
        test_links = ["https://example.com/a", "https://example.com/b"]
        mark_links_as_sent(test_links, data_dir=data_dir)

        # 3. 再次加载
        links = load_sent_links(days=3, data_dir=data_dir)
        assert len(links) == 2
        assert "https://example.com/a" in links
        assert "https://example.com/b" in links

        # 4. 测试过期清理
        # 手动写一个过期的记录
        history_file = temp_dir / "sent-history.json"
        old_time = (datetime.now() - timedelta(days=5)).isoformat()
        new_time = datetime.now().isoformat()
        
        with open(history_file, "w") as f:
            json.dump({
                "https://example.com/expired": old_time,
                "https://example.com/fresh": new_time
            }, f)

        # 加载 3 天内的已发送
        fresh_links = load_sent_links(days=3, data_dir=data_dir)
        assert "https://example.com/fresh" in fresh_links
        assert "https://example.com/expired" not in fresh_links

