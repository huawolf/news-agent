# Extra Sections (GitHub Trending / Hacker News / Insights) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add three new content sections (GitHub Trending, Hacker News, cross-section Insights) to the morning push, while keeping evening push as RSS-only and RSS as the core load-bearing module.

**Architecture:** Each section is an autonomous module under `src/sections/<board>/` returning `(markdown, error)`. `push_job` orchestrates four modules (RSS / GitHub / HN run in parallel via `asyncio.gather`; Insights runs after, consuming the three section outputs), wraps each output with HTML-comment sentinels (`<!-- SECTION:xxx BEGIN/END -->`), and writes the assembled push file. Module failure (other than RSS) degrades to omitting that section. State is local files only — `news-data/trending-history.json` deduplicates GitHub repos across days.

**Tech Stack:** Python 3.12 / asyncio / aiohttp / BeautifulSoup4 (new) / croniter / markdownify / DeepSeek (OpenAI-compatible) LLM API / Algolia HN public API / GitHub REST API v3.

**Spec reference:** `docs/extra-sections-design.md`

---

## Phase 0: Prerequisites

### Task 0: Add BeautifulSoup4 dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add bs4 to dependencies**

Open `pyproject.toml` and add `"beautifulsoup4>=4.12.0",` to the `dependencies` list (after `"aiohttp>=3.9.0",`).

- [ ] **Step 2: Sync deps**

Run: `uv sync`
Expected: package installed, no errors.

- [ ] **Step 3: Verify import works**

Run: `uv run python -c "from bs4 import BeautifulSoup; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: add beautifulsoup4 for HTML parsing"
```

---

## Phase 1: Storage Layer Foundation

The storage layer adds sentinel-aware section extraction, a `TrendingHistory` class for GH dedup, and a `profile` field on push frontmatter. These changes are independent of any new section module and must land first.

### Task 1: Add `extract_section` to storage.py

Parses `<!-- SECTION:xxx BEGIN/END -->` boundaries. Backward-compatible: old push files without sentinel return whole body when `section="rss"`, empty otherwise.

**Files:**
- Modify: `src/storage.py` (append after `_extract_push_titles`)
- Test: `tests/pytest/test_storage_sections.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/pytest/test_storage_sections.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/pytest/test_storage_sections.py -v`
Expected: ImportError or `AttributeError: module 'storage' has no attribute 'extract_section'`

- [ ] **Step 3: Implement `extract_section`**

Append to `src/storage.py`:

```python
_SECTION_RE_CACHE: Dict[str, re.Pattern] = {}


def _section_re(section: str) -> re.Pattern:
    """获取/缓存 sentinel 正则。section 名做转义,允许字母数字下划线"""
    if section not in _SECTION_RE_CACHE:
        s = re.escape(section)
        pattern = rf"<!--\s*SECTION:{s}\s*BEGIN\s*-->(.*?)<!--\s*SECTION:{s}\s*END\s*-->"
        _SECTION_RE_CACHE[section] = re.compile(pattern, flags=re.DOTALL)
    return _SECTION_RE_CACHE[section]


def extract_section(push_md: str, section: str) -> str:
    """从 push 文件内容中切出 <!-- SECTION:{section} BEGIN/END --> 之间的 markdown。

    向后兼容:
    - 新文件(带 sentinel): 返回 sentinel 边界内的原文(不去边界空行)
    - 老文件(无 sentinel) 且 section == 'rss': 返回整个 push_md
    - 老文件(无 sentinel) 且 section != 'rss': 返回空字符串
    - sentinel 残缺(只有 BEGIN 没有 END): 返回空字符串
    """
    match = _section_re(section).search(push_md)
    if match:
        return match.group(1)

    # 老文件兜底:rss 段视为整个 body
    has_any_sentinel = "<!-- SECTION:" in push_md
    if section == "rss" and not has_any_sentinel:
        return push_md
    return ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/pytest/test_storage_sections.py::TestExtractSection -v`
Expected: 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/storage.py tests/pytest/test_storage_sections.py
git commit -m "feat(storage): add sentinel-aware extract_section with legacy fallback"
```

---

### Task 2: Add `load_recent_section_titles` to storage.py

Reuses the existing `_extract_push_titles` for the H3 scan, but scopes it to a single section via `extract_section`.

**Files:**
- Modify: `src/storage.py`
- Test: `tests/pytest/test_storage_sections.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/pytest/test_storage_sections.py`:

```python
from datetime import date, datetime, timedelta
from storage import load_recent_section_titles, save_push_file


class TestLoadRecentSectionTitles:
    def test_returns_empty_when_dir_missing(self, tmp_path):
        assert load_recent_section_titles("rss", 3, str(tmp_path / "missing")) == ""

    def test_returns_empty_when_no_files(self, tmp_path):
        assert load_recent_section_titles("rss", 3, str(tmp_path)) == ""

    def test_extracts_only_target_section_titles(self, tmp_path):
        from src.config import get_timezone

        today = datetime.now(get_timezone()).date()
        push_file = tmp_path / f"push-{today.isoformat()}-08-00-00.md"
        push_file.write_text(
            f'---\npushDate: "{datetime.now(get_timezone()).isoformat()}"\n---\n\n'
            "<!-- SECTION:rss BEGIN -->\n"
            "### 1️⃣ RSS Title One\n"
            "### 2️⃣ RSS Title Two\n"
            "<!-- SECTION:rss END -->\n\n"
            "<!-- SECTION:github BEGIN -->\n"
            "### GH Repo Title\n"
            "<!-- SECTION:github END -->\n",
            encoding="utf-8",
        )
        rss_titles = load_recent_section_titles("rss", 3, str(tmp_path))
        assert "RSS Title One" in rss_titles
        assert "RSS Title Two" in rss_titles
        assert "GH Repo Title" not in rss_titles

        gh_titles = load_recent_section_titles("github", 3, str(tmp_path))
        assert "GH Repo Title" in gh_titles
        assert "RSS Title One" not in gh_titles
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/pytest/test_storage_sections.py::TestLoadRecentSectionTitles -v`
Expected: `AttributeError` on the import.

- [ ] **Step 3: Implement `load_recent_section_titles`**

Append to `src/storage.py`:

```python
def load_recent_section_titles(
    section: str, days: int, data_dir: str = "news-data"
) -> str:
    """加载最近 days 天 push 文件中 section 段的标题清单(供 LLM 查重防风格趋同)。

    返回紧凑纯文本,每行一条事件;遇到老文件(无 sentinel)按 extract_section 的兜底语义处理。
    """
    data_path = Path(data_dir)
    if not data_path.exists():
        return ""

    tz = get_timezone()
    today = datetime.now(tz).date()

    items: List[tuple] = []
    loaded_files: List[str] = []
    for i in range(days):
        d = today - timedelta(days=i)
        pattern = f"push-{d.isoformat()}-*.md"
        for push_file in sorted(data_path.glob(pattern)):
            if push_file.stat().st_size == 0:
                continue
            try:
                with open(push_file, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                continue
            section_md = extract_section(content, section)
            if not section_md:
                continue
            items.extend(_extract_push_titles(section_md))
            loaded_files.append(push_file.name)

    if loaded_files:
        print(
            f"   📂 已加载 {len(loaded_files)} 个 push 文件 (section={section}): "
            f"{', '.join(loaded_files)}"
        )

    return "\n".join(f"- [{t}] {title}" if t else f"- {title}" for t, title in items)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/pytest/test_storage_sections.py::TestLoadRecentSectionTitles -v`
Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/storage.py tests/pytest/test_storage_sections.py
git commit -m "feat(storage): add load_recent_section_titles for section-scoped history"
```

---

### Task 3: Add `TrendingHistory` to storage.py

Owns `news-data/trending-history.json` read / write / touch / cleanup. Single source of truth for GH dedup state.

**Files:**
- Modify: `src/storage.py`
- Test: `tests/pytest/test_storage_sections.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/pytest/test_storage_sections.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/pytest/test_storage_sections.py::TestTrendingHistory -v`
Expected: import error.

- [ ] **Step 3: Implement `TrendingHistory` + loader**

Append to `src/storage.py`:

```python
class TrendingHistory:
    """GitHub trending 已查阅 repo 索引。

    repos 字段:url → last_seen_date (ISO YYYY-MM-DD)。
    每次早报 cleanup 一次,touch 完所有今日 URL 后 save。
    """

    def __init__(self, path: str, repos: Dict[str, str]):
        self._path = path
        self.repos: Dict[str, str] = dict(repos)

    def __contains__(self, url: str) -> bool:
        return url in self.repos

    def touch(self, url: str, today: date) -> None:
        self.repos[url] = today.isoformat()

    def cleanup(self, today: date, keep_days: int) -> None:
        cutoff = today - timedelta(days=keep_days)
        self.repos = {
            url: d
            for url, d in self.repos.items()
            if _parse_iso_date_safe(d) is not None
            and _parse_iso_date_safe(d) >= cutoff
        }

    def save(self) -> None:
        path = Path(self._path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "repos": self.repos,
            "updated_at": datetime.now(get_timezone()).isoformat(),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)


def _parse_iso_date_safe(s: str) -> Optional[date]:
    try:
        return date.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def load_trending_history(path: str) -> TrendingHistory:
    """读取 trending-history.json;不存在返回空实例。"""
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return TrendingHistory(path, {})
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return TrendingHistory(path, data.get("repos", {}))
    except (json.JSONDecodeError, OSError):
        print(f"⚠️ trending-history 读取失败,使用空索引: {path}")
        return TrendingHistory(path, {})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/pytest/test_storage_sections.py::TestTrendingHistory -v`
Expected: 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/storage.py tests/pytest/test_storage_sections.py
git commit -m "feat(storage): add TrendingHistory for GH repo dedup state"
```

---

### Task 4: Add `profile` field to `save_push_file`

Push frontmatter gets a `profile: "morning"|"default"` tag so downstream tools can filter.

**Files:**
- Modify: `src/storage.py:363-378` (the existing `save_push_file`)
- Test: `tests/pytest/test_storage_sections.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/pytest/test_storage_sections.py`:

```python
from storage import save_push_file


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/pytest/test_storage_sections.py::TestSavePushFileProfile -v`
Expected: FAIL (current `save_push_file` doesn't accept `profile`).

- [ ] **Step 3: Update `save_push_file` signature**

Open `src/storage.py`. Replace the `save_push_file` function (currently around line 363-378) with:

```python
def save_push_file(
    filepath: str,
    content: str,
    source_count: int,
    total_entries: int,
    profile: str = "default",
):
    """保存推送文件（Markdown格式）

    Args:
        profile: "morning" | "default"  ← 早报或常规;写入 frontmatter,便于按 profile 分析
    """
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)

    push_time = datetime.now(get_timezone())
    frontmatter = (
        "---\n"
        f'pushDate: "{push_time.isoformat()}"\n'
        f'profile: "{profile}"\n'
        f"sourceCount: {source_count}\n"
        f"totalEntries: {total_entries}\n"
        "---\n\n"
    )

    with open(path, "w", encoding="utf-8") as f:
        f.write(frontmatter + content)
```

- [ ] **Step 4: Verify existing storage tests still pass**

Run: `uv run pytest tests/pytest/test_storage.py tests/pytest/test_storage_sections.py -v`
Expected: all pass (existing callers pass no `profile`, default "default" kicks in).

- [ ] **Step 5: Commit**

```bash
git add src/storage.py tests/pytest/test_storage_sections.py
git commit -m "feat(storage): add profile field to push frontmatter"
```

---

### Task 5: Extend `cleanup_old_files` to prune expired entries in `trending-history.json`

The fetch job's daily cleanup should not delete the trending-history file (it's cumulative state), but should remove entries older than `keep_days`.

**Files:**
- Modify: `src/storage.py:419-451` (the existing `cleanup_old_files`)
- Test: `tests/pytest/test_storage_sections.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/pytest/test_storage_sections.py`:

```python
from storage import cleanup_old_files


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
        import json as _j
        data = _j.loads(path.read_text(encoding="utf-8"))
        assert "https://github.com/a/b" not in data["repos"]
        assert "https://github.com/c/d" in data["repos"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/pytest/test_storage_sections.py::TestCleanupOldFilesTrendingHistory -v`
Expected: FAIL (current cleanup doesn't touch trending-history).

- [ ] **Step 3: Update `cleanup_old_files`**

Open `src/storage.py`. After the existing `for pattern in [...]` loop (around line 428-447), but **before** the final `if deleted_count > 0:` print, insert:

```python
    # trending-history.json: 剪枝过期条目,保留文件本身
    trending_path = data_path / "trending-history.json"
    if trending_path.exists() and trending_path.stat().st_size > 0:
        try:
            history = load_trending_history(str(trending_path))
            before = len(history.repos)
            history.cleanup(today=datetime.now().date(), keep_days=days)
            after = len(history.repos)
            if after < before:
                history.save()
                print(f"   ✂️ trending-history 剪枝: {before} → {after} 条")
        except Exception as e:
            print(f"   ⚠️ trending-history 剪枝失败: {e}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/pytest/test_storage_sections.py::TestCleanupOldFilesTrendingHistory tests/pytest/test_storage.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/storage.py tests/pytest/test_storage_sections.py
git commit -m "feat(storage): prune expired entries in trending-history.json on cleanup"
```

---

## Phase 2: RSS Module Migration

Migrate the existing RSS push flow into the new `sections` package without changing behavior.

### Task 6: Create `src/sections/rss/section.py`

`run_rss_section(config, now)` wraps existing `collect_entries_for_push` + `compose_digest` and returns `(markdown, error)`.

**Files:**
- Create: `src/sections/__init__.py` (empty)
- Create: `src/sections/rss/__init__.py` (empty)
- Create: `src/sections/rss/section.py`
- Test: `tests/pytest/test_sections_rss.py`

- [ ] **Step 1: Write the failing test**

Create `tests/pytest/test_sections_rss.py`:

```python
"""测试 RSS 模块返回 (markdown, error) 契约"""

import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.sections.rss.section import run_rss_section


@pytest.mark.asyncio
async def test_returns_markdown_when_entries_present(sample_config, tmp_path):
    with patch(
        "src.sections.rss.section.collect_entries_for_push",
        return_value=([{"link": "x", "title": "t", "score": 80}], []),
    ), patch(
        "src.sections.rss.section.compose_digest",
        new=AsyncMock(return_value="# digest body"),
    ), patch(
        "src.sections.rss.section.load_recent_push_titles", return_value=""
    ), patch(
        "src.sections.rss.section.get_last_push_file", return_value=None
    ):
        md, err = await run_rss_section(sample_config, now=None)

    assert md == "# digest body"
    assert err is None


@pytest.mark.asyncio
async def test_returns_empty_when_no_entries(sample_config):
    with patch(
        "src.sections.rss.section.collect_entries_for_push", return_value=([], [])
    ), patch(
        "src.sections.rss.section.get_last_push_file", return_value=None
    ):
        md, err = await run_rss_section(sample_config, now=None)

    assert md == ""
    assert err is None


@pytest.mark.asyncio
async def test_returns_error_on_compose_failure(sample_config):
    with patch(
        "src.sections.rss.section.collect_entries_for_push",
        return_value=([{"link": "x"}], []),
    ), patch(
        "src.sections.rss.section.compose_digest",
        new=AsyncMock(side_effect=RuntimeError("LLM down")),
    ), patch(
        "src.sections.rss.section.load_recent_push_titles", return_value=""
    ), patch(
        "src.sections.rss.section.get_last_push_file", return_value=None
    ):
        md, err = await run_rss_section(sample_config, now=None)

    assert md == ""
    assert "LLM down" in err
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/pytest/test_sections_rss.py -v`
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement module**

Create `src/sections/__init__.py`:

```python
"""板块模块包。每个子模块导出 run_<board>_section(config, now) -> (markdown, error)"""
```

Create `src/sections/rss/__init__.py`:

```python
from src.sections.rss.section import run_rss_section

__all__ = ["run_rss_section"]
```

Create `src/sections/rss/section.py`:

```python
"""RSS 板块:沿用现有 collect_entries_for_push + compose_digest 流程

迁移自 src/main.py::run_push_job 中 RSS digest 部分,行为保持一致。
"""

from datetime import datetime
from typing import Dict, Optional, Tuple

from src.llm import compose_digest
from src.storage import (
    extract_push_time,
    get_last_push_file,
    load_recent_push_titles,
)


async def run_rss_section(
    config: Dict, now: Optional[datetime] = None
) -> Tuple[str, Optional[str]]:
    """生成 RSS digest markdown 段(不含 sentinel)。

    返回:
        (markdown, error)
        - 无新内容时返回 ("", None)
        - compose_digest 失败时返回 ("", error_message)
    """
    # 延迟 import 避免循环引用:collect_entries_for_push 仍在 main.py
    from src.main import collect_entries_for_push

    last_push_file = get_last_push_file()
    last_push_time = extract_push_time(last_push_file) if last_push_file else None

    min_score = config["filter"]["min_score"]
    context_days = config["filter"]["context_days"]

    to_push, context = collect_entries_for_push(
        last_push_time=last_push_time,
        context_days=context_days,
        min_score=min_score,
    )

    if not to_push:
        print("ℹ️ RSS: 无新消息")
        return "", None

    push_context_days = config["filter"].get("push_context_days", 5)
    recent = load_recent_push_titles(push_context_days)

    try:
        md = await compose_digest(to_push, context, config["llm"], recent_push_context=recent)
        return md, None
    except Exception as e:
        msg = f"compose_digest 失败: {e}"
        print(f"⚠️ RSS: {msg}")
        return "", msg
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/pytest/test_sections_rss.py -v`
Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/sections/ tests/pytest/test_sections_rss.py
git commit -m "feat(sections): extract RSS digest flow into run_rss_section"
```

---

## Phase 3: GitHub Module

### Task 7: Save a real GitHub trending HTML fixture

Save a real snapshot for deterministic parser tests. The structure may drift; this is the canonical "what we built against".

**Files:**
- Create: `tests/pytest/fixtures/github_trending.html`

- [ ] **Step 1: Download a real page**

```bash
mkdir -p tests/pytest/fixtures
curl -fsSL -A "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36" \
  "https://github.com/trending" \
  -o tests/pytest/fixtures/github_trending.html
```

- [ ] **Step 2: Verify the fixture has expected markers**

Run: `grep -c 'class="Box-row"' tests/pytest/fixtures/github_trending.html`
Expected: a number ≥ 10 (typically 25). If 0, GitHub markup changed — adjust selectors in Task 8 accordingly.

- [ ] **Step 3: Commit fixture**

```bash
git add tests/pytest/fixtures/github_trending.html
git commit -m "test(github): snapshot github.com/trending fixture"
```

---

### Task 8: Implement `trending_scraper.py`

Async `fetch_trending_page` + `parse_trending_html` returning `[{url, full_name, description, language, stars_today, stars_total}]`.

**Files:**
- Create: `src/sections/github/__init__.py` (empty)
- Create: `src/sections/github/trending_scraper.py`
- Test: `tests/pytest/test_sections_github_scraper.py`

- [ ] **Step 1: Write the failing test**

Create `tests/pytest/test_sections_github_scraper.py`:

```python
"""测试 GitHub trending HTML 解析"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.sections.github.trending_scraper import parse_trending_html


def test_parse_trending_html_returns_repo_dicts():
    fixture = (
        Path(__file__).parent / "fixtures" / "github_trending.html"
    ).read_text(encoding="utf-8")

    repos = parse_trending_html(fixture)

    assert len(repos) > 0
    first = repos[0]
    assert first["url"].startswith("https://github.com/")
    assert "/" in first["full_name"]
    assert isinstance(first["stars_today"], int)
    assert isinstance(first["stars_total"], int)
    # description / language 可为空字符串但必须是 str
    assert isinstance(first["description"], str)
    assert isinstance(first["language"], str)


def test_parse_trending_html_dedupes_by_url():
    fixture = (
        Path(__file__).parent / "fixtures" / "github_trending.html"
    ).read_text(encoding="utf-8")
    repos = parse_trending_html(fixture)
    urls = [r["url"] for r in repos]
    assert len(urls) == len(set(urls))


def test_parse_trending_html_empty_input():
    assert parse_trending_html("") == []
    assert parse_trending_html("<html><body>no repos</body></html>") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/pytest/test_sections_github_scraper.py -v`
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement scraper**

Create `src/sections/github/__init__.py` (empty):

```python
```

Create `src/sections/github/trending_scraper.py`:

```python
"""GitHub Trending 单页 HTML 抓取与解析。

数据源: https://github.com/trending (无 language / since 过滤)
"""

import re
from typing import Dict, List

import aiohttp
from bs4 import BeautifulSoup

TRENDING_URL = "https://github.com/trending"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_NUM_RE = re.compile(r"[\d,]+")


def _parse_int(s: str) -> int:
    m = _NUM_RE.search(s or "")
    if not m:
        return 0
    return int(m.group(0).replace(",", ""))


async def fetch_trending_page(timeout: int = 10) -> str:
    """抓取 trending 页 HTML;非 200 抛 RuntimeError"""
    async with aiohttp.ClientSession(
        headers={"User-Agent": USER_AGENT}
    ) as session:
        async with session.get(
            TRENDING_URL, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(
                    f"GitHub trending 返回 {resp.status}: {await resp.text()[:200]}"
                )
            return await resp.text()


def parse_trending_html(html: str) -> List[Dict]:
    """解析 trending HTML,返回去重后的 repo 字典数组。"""
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    seen_urls = set()
    repos: List[Dict] = []

    for article in soup.select("article.Box-row"):
        h2 = article.find("h2")
        a = h2.find("a") if h2 else None
        if not a or not a.get("href"):
            continue

        href = a["href"].strip()
        full_name = href.lstrip("/")
        url = f"https://github.com/{full_name}"
        if url in seen_urls:
            continue
        seen_urls.add(url)

        # description
        desc_tag = article.find("p")
        description = (desc_tag.get_text(strip=True) if desc_tag else "") or ""

        # language
        lang_tag = article.find("span", attrs={"itemprop": "programmingLanguage"})
        language = (lang_tag.get_text(strip=True) if lang_tag else "") or ""

        # stars_total: 第一个指向 /stargazers 的链接
        stars_total = 0
        star_a = article.find("a", href=re.compile(r"/stargazers$"))
        if star_a:
            stars_total = _parse_int(star_a.get_text(strip=True))

        # stars_today: 末尾的 "N stars today" span (class 多变,按文本)
        stars_today = 0
        for span in article.find_all("span"):
            t = span.get_text(strip=True)
            if "stars today" in t or "stars this week" in t or "stars this month" in t:
                stars_today = _parse_int(t)
                break

        repos.append(
            {
                "url": url,
                "full_name": full_name,
                "description": description,
                "language": language,
                "stars_today": stars_today,
                "stars_total": stars_total,
            }
        )

    return repos
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/pytest/test_sections_github_scraper.py -v`
Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/sections/github/ tests/pytest/test_sections_github_scraper.py
git commit -m "feat(github): add trending page scraper with HTML fixture test"
```

---

### Task 9: Implement `repo_enricher.py`

Two REST API calls per repo (`/repos/{o}/{r}` + `/repos/{o}/{r}/readme`). Token via env var optional.

**Files:**
- Create: `src/sections/github/repo_enricher.py`
- Test: `tests/pytest/test_sections_github_enricher.py`

- [ ] **Step 1: Write the failing test**

Create `tests/pytest/test_sections_github_enricher.py`:

```python
"""测试 GitHub REST API enrich 字段映射、archived 过滤、token 鉴权头"""

import base64
import os
import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.sections.github.repo_enricher import enrich_repo, _auth_headers


def test_auth_headers_with_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    headers = _auth_headers(token_env="GITHUB_TOKEN")
    assert headers["Authorization"] == "Bearer ghp_secret"
    assert headers["Accept"] == "application/vnd.github+json"


def test_auth_headers_without_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    headers = _auth_headers(token_env="GITHUB_TOKEN")
    assert "Authorization" not in headers
    assert headers["Accept"] == "application/vnd.github+json"


@pytest.mark.asyncio
async def test_enrich_repo_merges_metadata_and_readme():
    readme_body = "# Title\n\nProject description here."
    readme_b64 = base64.b64encode(readme_body.encode("utf-8")).decode("ascii")

    metadata_payload = {
        "description": "real desc",
        "topics": ["llm", "rag"],
        "license": {"spdx_id": "MIT"},
        "pushed_at": "2026-05-16T10:00:00Z",
        "archived": False,
    }
    readme_payload = {"content": readme_b64, "encoding": "base64"}

    async def fake_get_json(session, url, **kwargs):
        if url.endswith("/readme"):
            return readme_payload
        return metadata_payload

    base = {
        "url": "https://github.com/o/r",
        "full_name": "o/r",
        "description": "from trending",
        "language": "Python",
        "stars_today": 100,
        "stars_total": 5000,
    }

    with patch(
        "src.sections.github.repo_enricher._get_json", new=AsyncMock(side_effect=fake_get_json)
    ):
        enriched = await enrich_repo(
            session=MagicMock(), repo=base, token_env="GITHUB_TOKEN", readme_max_chars=200
        )

    assert enriched["topics"] == ["llm", "rag"]
    assert enriched["license"] == "MIT"
    assert enriched["pushed_at"] == "2026-05-16T10:00:00Z"
    assert "Project description" in enriched["readme_excerpt"]
    # trending 已有字段保留
    assert enriched["stars_today"] == 100


@pytest.mark.asyncio
async def test_enrich_repo_returns_none_when_archived():
    metadata_payload = {"archived": True, "topics": [], "pushed_at": "x"}

    async def fake_get_json(session, url, **kwargs):
        if url.endswith("/readme"):
            return {"content": ""}
        return metadata_payload

    base = {"url": "https://github.com/o/r", "full_name": "o/r"}
    with patch(
        "src.sections.github.repo_enricher._get_json", new=AsyncMock(side_effect=fake_get_json)
    ):
        result = await enrich_repo(
            session=MagicMock(), repo=base, token_env="GITHUB_TOKEN", readme_max_chars=200
        )
    assert result is None


@pytest.mark.asyncio
async def test_enrich_repo_truncates_readme():
    readme_body = "x" * 5000
    readme_b64 = base64.b64encode(readme_body.encode("utf-8")).decode("ascii")

    async def fake_get_json(session, url, **kwargs):
        if url.endswith("/readme"):
            return {"content": readme_b64, "encoding": "base64"}
        return {"archived": False, "topics": [], "pushed_at": "p"}

    base = {"url": "https://github.com/o/r", "full_name": "o/r"}
    with patch(
        "src.sections.github.repo_enricher._get_json", new=AsyncMock(side_effect=fake_get_json)
    ):
        enriched = await enrich_repo(
            session=MagicMock(), repo=base, token_env="GITHUB_TOKEN", readme_max_chars=100
        )
    assert len(enriched["readme_excerpt"]) == 100
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/pytest/test_sections_github_enricher.py -v`
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement enricher**

Create `src/sections/github/repo_enricher.py`:

```python
"""GitHub REST API enrich:metadata + README → enriched repo dict

匿名调用受 60 req/hr 限,设置 GITHUB_TOKEN 环境变量后走 5000 req/hr。
"""

import asyncio
import base64
import os
from typing import Dict, List, Optional, Tuple

import aiohttp

API_BASE = "https://api.github.com"


def _auth_headers(token_env: str = "GITHUB_TOKEN") -> Dict[str, str]:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get(token_env)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _get_json(
    session: aiohttp.ClientSession, url: str, timeout: int = 10
) -> Optional[Dict]:
    async with session.get(
        url, timeout=aiohttp.ClientTimeout(total=timeout)
    ) as resp:
        if resp.status == 404:
            return None
        if resp.status != 200:
            raise RuntimeError(f"GitHub API {resp.status} for {url}")
        return await resp.json()


async def enrich_repo(
    session: aiohttp.ClientSession,
    repo: Dict,
    token_env: str = "GITHUB_TOKEN",
    readme_max_chars: int = 3000,
    timeout: int = 10,
) -> Optional[Dict]:
    """单 repo 双调用 enrich。返回 None 表示该 repo 应剔除(archived 或 metadata 不可达)。

    任一调用失败 raise → 调用方按 return_exceptions 模式聚合错误。
    """
    full_name = repo["full_name"]
    meta_url = f"{API_BASE}/repos/{full_name}"
    readme_url = f"{API_BASE}/repos/{full_name}/readme"

    meta, readme = await asyncio.gather(
        _get_json(session, meta_url, timeout=timeout),
        _get_json(session, readme_url, timeout=timeout),
    )

    if meta is None:
        return None
    if meta.get("archived"):
        return None

    license_spdx = ""
    if isinstance(meta.get("license"), dict):
        license_spdx = meta["license"].get("spdx_id") or ""

    readme_excerpt = ""
    if readme and readme.get("content"):
        try:
            raw = base64.b64decode(readme["content"]).decode("utf-8", errors="replace")
            readme_excerpt = raw[:readme_max_chars]
        except Exception:
            readme_excerpt = ""

    return {
        **repo,
        "topics": meta.get("topics") or [],
        "license": license_spdx,
        "pushed_at": meta.get("pushed_at") or "",
        "readme_excerpt": readme_excerpt,
    }


async def enrich_repos(
    candidates: List[Dict],
    token_env: str = "GITHUB_TOKEN",
    readme_max_chars: int = 3000,
    timeout: int = 10,
) -> Tuple[List[Dict], List[str]]:
    """并发 enrich 多个 repo。返回 (enriched_list_with_archived_filtered, errors)"""
    errors: List[str] = []
    headers = _auth_headers(token_env)

    async with aiohttp.ClientSession(headers=headers) as session:
        results = await asyncio.gather(
            *[
                enrich_repo(session, r, token_env, readme_max_chars, timeout)
                for r in candidates
            ],
            return_exceptions=True,
        )

    enriched: List[Dict] = []
    for r, candidate in zip(results, candidates):
        if isinstance(r, Exception):
            errors.append(f"enrich {candidate['full_name']} 失败: {r}")
        elif r is not None:
            enriched.append(r)
    return enriched, errors
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/pytest/test_sections_github_enricher.py -v`
Expected: 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/sections/github/repo_enricher.py tests/pytest/test_sections_github_enricher.py
git commit -m "feat(github): add repo_enricher using REST API metadata + readme"
```

---

### Task 10: Implement `src/sections/github/section.py`

Glue: scrape → cleanup history → filter → touch new → save history → enrich → LLM summarize.

**Files:**
- Create: `src/sections/github/section.py`
- Test: `tests/pytest/test_sections_github_section.py`

- [ ] **Step 1: Write the failing test**

Create `tests/pytest/test_sections_github_section.py`:

```python
"""测试 GitHub 板块编排:抓取 → history 过滤 → enrich → LLM 总结"""

import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.sections.github.section import run_github_section


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
    # 预置 history,使得所有今日 scrape 出来的 repo 都已存在
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
        "src.sections.github.section.summarize_github_trending",
        new=AsyncMock(return_value=("## GH section md", None)),
    ):
        md, err = await run_github_section(cfg, now=None)

    assert md == "## GH section md"
    assert err is None
    # history 应已写入今日 scrape 出的 URL
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/pytest/test_sections_github_section.py -v`
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement section orchestrator**

Create `src/sections/github/section.py`:

```python
"""GitHub Trending 板块入口。

流程:trending 抓取 → history 过滤 → 候选写回 history → deep-dive → LLM 总结
"""

from datetime import datetime
from typing import Dict, Optional, Tuple

from src.config import get_timezone
from src.sections.github.repo_enricher import enrich_repos
from src.sections.github.trending_scraper import (
    fetch_trending_page,
    parse_trending_html,
)
from src.storage import load_trending_history


async def run_github_section(
    config: Dict, now: Optional[datetime] = None
) -> Tuple[str, Optional[str]]:
    cfg = config.get("sections", {}).get("github_trending", {})
    if not cfg.get("enabled", False):
        return "", None

    # 延迟 import 避免循环
    from src.llm import summarize_github_trending

    today = (now or datetime.now(get_timezone())).date()
    keep_days = config["filter"]["keep_days"]
    timeout = cfg.get("request_timeout", 10)
    max_deep_dive = cfg.get("max_deep_dive", 10)
    readme_max_chars = cfg.get("readme_max_chars", 3000)
    history_path = cfg.get("history_file", "news-data/trending-history.json")
    token_env = cfg.get("tokenName", "GITHUB_TOKEN")

    # 1. 抓取
    try:
        html = await fetch_trending_page(timeout=timeout)
    except Exception as e:
        return "", f"GH 抓取失败: {e}"

    all_repos = parse_trending_html(html)
    if not all_repos:
        return "", None

    # 2. history 加载 + 清理
    history = load_trending_history(history_path)
    history.cleanup(today=today, keep_days=keep_days)

    # 3. 候选筛选
    candidates = []
    for repo in all_repos:
        if repo["url"] in history:
            history.touch(repo["url"], today)
        else:
            candidates.append(repo)

    # 4. 候选写回 history + 持久化
    for repo in candidates:
        history.touch(repo["url"], today)
    history.save()

    if not candidates:
        return "", None
    if len(candidates) > max_deep_dive:
        candidates = candidates[:max_deep_dive]

    # 5. 并发 enrich
    enriched, enrich_errors = await enrich_repos(
        candidates,
        token_env=token_env,
        readme_max_chars=readme_max_chars,
        timeout=timeout,
    )
    for e in enrich_errors:
        print(f"⚠️ GH enrich: {e}")
    if not enriched:
        return "", None

    # 6. LLM 总结
    md, err = await summarize_github_trending(enriched, config["llm"])
    if err:
        return "", f"summarize_github_trending: {err}"
    return md or "", None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/pytest/test_sections_github_section.py -v`
Expected: 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/sections/github/section.py tests/pytest/test_sections_github_section.py
git commit -m "feat(github): orchestrate trending → history → enrich → LLM"
```

---

### Task 11: Add `summarize_github_trending` to `src/llm.py` + `prompts/section_github.md`

**Files:**
- Modify: `src/llm.py` (append new async function)
- Create: `prompts/section_github.md`
- Test: `tests/pytest/test_sections_github_section.py` (already mocks the function; add direct test)

- [ ] **Step 1: Write the failing test**

Create `tests/pytest/test_llm_extra_sections.py`:

```python
"""测试新增 LLM 函数 (summarize_github_trending 等)"""

import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from llm import summarize_github_trending


@pytest.mark.asyncio
async def test_summarize_github_trending_happy_path(tmp_path):
    prompt_path = tmp_path / "section_github.md"
    prompt_path.write_text("Repos: {repos_json}\nmax_items={max_items}", encoding="utf-8")

    config = {
        "model": "x",
        "baseUrl": "http://x",
        "apiKeyName": "DEEPSEEK_API_KEY",
        "prompts": {"section_github": str(prompt_path)},
        "sections": {"github_trending": {"max_items": 3}},
    }
    enriched = [{"full_name": "o/r", "readme_excerpt": "rm"}]

    with patch("llm.call_llm", new=AsyncMock(return_value="## md")):
        md, err = await summarize_github_trending(enriched, config)

    assert md == "## md"
    assert err is None


@pytest.mark.asyncio
async def test_summarize_github_trending_llm_failure_returns_error(tmp_path):
    prompt_path = tmp_path / "section_github.md"
    prompt_path.write_text("x {repos_json} {max_items}", encoding="utf-8")
    config = {
        "model": "x",
        "baseUrl": "http://x",
        "apiKeyName": "DEEPSEEK_API_KEY",
        "prompts": {"section_github": str(prompt_path)},
        "sections": {"github_trending": {"max_items": 3}},
    }
    with patch("llm.call_llm", new=AsyncMock(side_effect=RuntimeError("boom"))):
        md, err = await summarize_github_trending([{"full_name": "o/r"}], config)
    assert md == ""
    assert "boom" in err
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/pytest/test_llm_extra_sections.py::test_summarize_github_trending_happy_path -v`
Expected: ImportError.

- [ ] **Step 3: Implement function + prompt**

Append to `src/llm.py`:

```python
async def summarize_github_trending(
    enriched_repos: List[Dict], config: Dict
) -> Tuple[str, Optional[str]]:
    """GH 板块总结:从 enriched 候选中选 1-max_items + 写 markdown。不传历史上下文。"""
    prompt_path = config.get("prompts", {}).get(
        "section_github", "prompts/section_github.md"
    )
    max_items = (
        config.get("sections", {}).get("github_trending", {}).get("max_items", 3)
    )
    prompt = load_prompt(
        prompt_path,
        repos_json=json.dumps(enriched_repos, ensure_ascii=False, indent=2),
        max_items=max_items,
    )
    try:
        return await call_llm(prompt, config), None
    except Exception as e:
        msg = f"summarize_github_trending 失败: {e}"
        print(f"⚠️ {msg}")
        return "", msg
```

Create `prompts/section_github.md`:

```markdown
你是开源情报分析师。从下列 GitHub Trending 候选项目中,挑出 **1-{max_items} 个**最值得关注的 AI 相关项目并行文。

## 关注领域(正面列表)
- **AI Agent**:智能体架构、工具链、多智能体、自主规划、Agent 框架
- **AI 模型**:训练、推理、微调、量化部署、模型服务、语音/多模态/视觉模型
- **AI 基础设施**:GPU 调度、芯片硬件、数据中心、推理优化、分布式训练、向量数据库、RAG 框架
- **大厂/前沿动态**:Apple、Google、Meta、OpenAI、Anthropic、Microsoft、xAI 等公司的官方动作与战略
- **AI 集成的开发者工具**:API 网关、自动化脚本、低代码平台等明确与 AI 协同的工具
- **创新性开源产品**:日增长显著且有清晰用户价值

## 排除(负面列表)
- 嵌入式开发(Arduino、ESP32、树莓派、单片机)
- 底层系统编程(内存分配器、编译器、链接器,与 AI 工作负载无关时)
- 通用开发工具(命名规范、代码风格、纯前端模板、UI 组件库、管理后台模板、静态网站主题)
- 学习资源(纯教程仓库、面试题合集、Roadmap,除非含实用代码的深度技术指南)
- 配置文件集合(Dotfiles、配置模板)
- 与 AI/科技无关的内容(电子书、资源搬运、刷榜项目)
- 纯娱乐/高风险误用(deepfake 等无明确基础设施价值)

## 输入数据
JSON 数组,字段:url / full_name / description / language / stars_today / stars_total / topics / license / pushed_at / readme_excerpt

```json
{repos_json}
```

## 选项规则
- 优先信号:`stars_today` 高 + `topics` 含 AI 标签(agent/llm/rag/inference/training 等) + readme 描述明确
- 跳过:`archived=true`(若漏过)、纯 awesome-list、个人 dotfiles
- 一句话价值定位需点明"解决什么问题",避免营销语("震撼""炸裂""革命性"等禁用)

## 输出格式(严格 Markdown,不要任何引导语)

```markdown
## ⭐ GitHub 趋势

- **owner/repo** ⭐{{stars_today}} — 一句话价值定位 [link]({{url}})
- ...
```

若候选中没有任何符合关注领域的项目,直接输出 `## ⭐ GitHub 趋势\n\n- 今日无显著 AI 相关趋势`,不要硬编。
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/pytest/test_llm_extra_sections.py::test_summarize_github_trending_happy_path tests/pytest/test_llm_extra_sections.py::test_summarize_github_trending_llm_failure_returns_error -v`
Expected: 2 pass.

- [ ] **Step 5: Commit**

```bash
git add src/llm.py prompts/section_github.md tests/pytest/test_llm_extra_sections.py
git commit -m "feat(llm): add summarize_github_trending with prompt"
```

---

## Phase 4: Hacker News Module

### Task 12: Save HN frontpage HTML fixture

**Files:**
- Create: `tests/pytest/fixtures/hn_frontpage.html`

- [ ] **Step 1: Download**

```bash
curl -fsSL -A "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36" \
  "https://news.ycombinator.com/news" \
  -o tests/pytest/fixtures/hn_frontpage.html
```

- [ ] **Step 2: Verify**

Run: `grep -c 'class="athing"' tests/pytest/fixtures/hn_frontpage.html`
Expected: ≥ 25 (usually 30).

- [ ] **Step 3: Commit**

```bash
git add tests/pytest/fixtures/hn_frontpage.html
git commit -m "test(hn): snapshot HN frontpage fixture"
```

---

### Task 13: Implement `frontpage_scraper.py`

**Files:**
- Create: `src/sections/hackernews/__init__.py` (empty)
- Create: `src/sections/hackernews/frontpage_scraper.py`
- Test: `tests/pytest/test_sections_hackernews_scraper.py`

- [ ] **Step 1: Write the failing test**

Create `tests/pytest/test_sections_hackernews_scraper.py`:

```python
"""测试 HN 首页 HTML 解析"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.sections.hackernews.frontpage_scraper import parse_frontpage_html


def test_parse_frontpage_returns_stories():
    fixture = (
        Path(__file__).parent / "fixtures" / "hn_frontpage.html"
    ).read_text(encoding="utf-8")

    stories = parse_frontpage_html(fixture)

    assert len(stories) >= 25
    s = stories[0]
    assert s["id"]
    assert s["title"]
    assert s["url"]
    assert isinstance(s["points"], int)
    assert isinstance(s["comments"], int)
    assert s["comments_url"].startswith("https://news.ycombinator.com/item?id=")


def test_parse_frontpage_detects_show_hn_internal_url():
    # 构造一个最小内部链接故事 (Ask HN / Show HN)
    html = """
    <table>
      <tr class="athing" id="111">
        <td class="title">
          <span class="titleline">
            <a href="item?id=111">Ask HN: what's new?</a>
          </span>
        </td>
      </tr>
      <tr>
        <td class="subtext">
          <span class="subline">
            <span class="score">50 points</span>
            by <a href="user?id=alice">alice</a>
            <span class="age"><a href="item?id=111">2 hours ago</a></span>
            | <a href="item?id=111">5&nbsp;comments</a>
          </span>
        </td>
      </tr>
    </table>
    """
    stories = parse_frontpage_html(html)
    assert len(stories) == 1
    s = stories[0]
    assert s["id"] == "111"
    assert s["url"].startswith("https://news.ycombinator.com/item?id=")
    assert s["site"] == ""
    assert s["points"] == 50
    assert s["comments"] == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/pytest/test_sections_hackernews_scraper.py -v`
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement**

Create `src/sections/hackernews/__init__.py` (empty).

Create `src/sections/hackernews/frontpage_scraper.py`:

```python
"""HN 首页 HTML 抓取与解析。

数据源: https://news.ycombinator.com/news (30 条)
"""

import re
from typing import Dict, List

import aiohttp
from bs4 import BeautifulSoup

FRONTPAGE_URL = "https://news.ycombinator.com/news"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_NUM_RE = re.compile(r"\d+")


def _first_int(text: str) -> int:
    m = _NUM_RE.search(text or "")
    return int(m.group(0)) if m else 0


async def fetch_frontpage(timeout: int = 10) -> str:
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        async with session.get(
            FRONTPAGE_URL, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HN frontpage 返回 {resp.status}")
            return await resp.text()


def parse_frontpage_html(html: str) -> List[Dict]:
    """解析首页 HTML,返回 [{id, title, url, site, points, comments, comments_url}]"""
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    stories: List[Dict] = []

    for athing in soup.select("tr.athing"):
        item_id = athing.get("id")
        if not item_id:
            continue

        title_a = athing.select_one("span.titleline > a")
        if not title_a:
            continue
        title = title_a.get_text(strip=True)
        href = title_a.get("href", "")
        # 内部链接(Ask HN / Show HN)
        if href.startswith("item?id="):
            url = f"https://news.ycombinator.com/{href}"
            site = ""
        else:
            url = href
            site_tag = athing.select_one("span.sitestr")
            site = site_tag.get_text(strip=True) if site_tag else ""

        # 同 id 的下一个 tr 是 subtext
        sub_tr = athing.find_next_sibling("tr")
        points = 0
        comments = 0
        comments_url = f"https://news.ycombinator.com/item?id={item_id}"
        if sub_tr:
            score = sub_tr.select_one("span.score")
            if score:
                points = _first_int(score.get_text(strip=True))
            # 最后一个 a[href^="item?id="] 是评论链接
            comment_a = None
            for a in sub_tr.find_all("a", href=re.compile(r"^item\?id=")):
                comment_a = a
            if comment_a:
                comments = _first_int(comment_a.get_text(strip=True))
                comments_url = f"https://news.ycombinator.com/{comment_a['href']}"

        stories.append(
            {
                "id": item_id,
                "title": title,
                "url": url,
                "site": site,
                "points": points,
                "comments": comments,
                "comments_url": comments_url,
            }
        )

    return stories
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/pytest/test_sections_hackernews_scraper.py -v`
Expected: 2 pass.

- [ ] **Step 5: Commit**

```bash
git add src/sections/hackernews/__init__.py src/sections/hackernews/frontpage_scraper.py tests/pytest/test_sections_hackernews_scraper.py
git commit -m "feat(hn): add frontpage scraper with fixture test"
```

---

### Task 14: Implement `item_enricher.py`

Algolia for comments + post text; `html_to_markdown` for external link content.

**Files:**
- Create: `src/sections/hackernews/item_enricher.py`
- Test: `tests/pytest/test_sections_hackernews_enricher.py`

- [ ] **Step 1: Write the failing test**

Create `tests/pytest/test_sections_hackernews_enricher.py`:

```python
"""测试 HN enrich(Algolia + 外链正文)"""

import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.sections.hackernews.item_enricher import enrich_story


@pytest.mark.asyncio
async def test_enrich_external_link_story():
    story = {
        "id": "111",
        "title": "T",
        "url": "https://example.com/post",
        "site": "example.com",
        "points": 100,
        "comments": 5,
        "comments_url": "https://news.ycombinator.com/item?id=111",
    }
    algolia_payload = {
        "text": None,
        "children": [
            {"text": "<p>comment one</p>"},
            {"text": "<p>comment two</p>"},
            {"text": "<p>comment three</p>"},
            {"text": "<p>comment four</p>"},
        ],
    }

    async def fake_algolia(session, item_id, **kw):
        return algolia_payload

    async def fake_link(session, url, **kw):
        return "<html><body><p>link body</p></body></html>"

    with patch(
        "src.sections.hackernews.item_enricher._fetch_algolia_item",
        new=AsyncMock(side_effect=fake_algolia),
    ), patch(
        "src.sections.hackernews.item_enricher._fetch_url_html",
        new=AsyncMock(side_effect=fake_link),
    ):
        enriched = await enrich_story(
            session=MagicMock(),
            story=story,
            top_comments=3,
            comment_max_chars=500,
            link_content_max_chars=3000,
            algolia_base="https://hn.algolia.com/api/v1",
            timeout=10,
        )

    assert len(enriched["top_comments"]) == 3
    assert "comment one" in enriched["top_comments"][0]
    assert "link body" in enriched["link_content"]


@pytest.mark.asyncio
async def test_enrich_show_hn_uses_root_text_no_external_fetch():
    story = {
        "id": "222",
        "title": "Show HN: T",
        "url": "https://news.ycombinator.com/item?id=222",
        "site": "",
        "points": 200,
        "comments": 10,
        "comments_url": "https://news.ycombinator.com/item?id=222",
    }
    algolia_payload = {
        "text": "<p>post body text</p>",
        "children": [{"text": "<p>c1</p>"}],
    }
    link_calls = []

    async def fake_algolia(session, item_id, **kw):
        return algolia_payload

    async def fake_link(session, url, **kw):
        link_calls.append(url)
        return "should not be called"

    with patch(
        "src.sections.hackernews.item_enricher._fetch_algolia_item",
        new=AsyncMock(side_effect=fake_algolia),
    ), patch(
        "src.sections.hackernews.item_enricher._fetch_url_html",
        new=AsyncMock(side_effect=fake_link),
    ):
        enriched = await enrich_story(
            session=MagicMock(),
            story=story,
            top_comments=3,
            comment_max_chars=500,
            link_content_max_chars=3000,
            algolia_base="https://hn.algolia.com/api/v1",
            timeout=10,
        )

    assert link_calls == []
    assert "post body text" in enriched["link_content"]


@pytest.mark.asyncio
async def test_enrich_truncates_comments_and_link():
    story = {
        "id": "333",
        "title": "T",
        "url": "https://example.com/a",
        "site": "example.com",
        "points": 100,
        "comments": 2,
        "comments_url": "x",
    }
    long_comment = "<p>" + ("y" * 2000) + "</p>"
    long_link = "<html><body>" + ("z" * 5000) + "</body></html>"

    async def fake_algolia(session, item_id, **kw):
        return {"text": None, "children": [{"text": long_comment}]}

    async def fake_link(session, url, **kw):
        return long_link

    with patch(
        "src.sections.hackernews.item_enricher._fetch_algolia_item",
        new=AsyncMock(side_effect=fake_algolia),
    ), patch(
        "src.sections.hackernews.item_enricher._fetch_url_html",
        new=AsyncMock(side_effect=fake_link),
    ):
        enriched = await enrich_story(
            session=MagicMock(),
            story=story,
            top_comments=3,
            comment_max_chars=100,
            link_content_max_chars=200,
            algolia_base="https://hn.algolia.com/api/v1",
            timeout=10,
        )

    assert len(enriched["top_comments"][0]) <= 100
    assert len(enriched["link_content"]) <= 200


@pytest.mark.asyncio
async def test_enrich_failure_returns_partial():
    story = {
        "id": "444",
        "title": "T",
        "url": "https://example.com/x",
        "site": "example.com",
        "points": 100,
        "comments": 2,
        "comments_url": "x",
    }

    async def fake_algolia(session, item_id, **kw):
        raise RuntimeError("algolia down")

    async def fake_link(session, url, **kw):
        return "<html><body>ok</body></html>"

    with patch(
        "src.sections.hackernews.item_enricher._fetch_algolia_item",
        new=AsyncMock(side_effect=fake_algolia),
    ), patch(
        "src.sections.hackernews.item_enricher._fetch_url_html",
        new=AsyncMock(side_effect=fake_link),
    ):
        enriched = await enrich_story(
            session=MagicMock(),
            story=story,
            top_comments=3,
            comment_max_chars=500,
            link_content_max_chars=3000,
            algolia_base="https://hn.algolia.com/api/v1",
            timeout=10,
        )

    # 算法失败 → top_comments 留空,link_content 仍获取
    assert enriched["top_comments"] == []
    assert "ok" in enriched["link_content"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/pytest/test_sections_hackernews_enricher.py -v`
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement**

Create `src/sections/hackernews/item_enricher.py`:

```python
"""HN 单 story enrich:Algolia 评论 + 外链正文。

Algolia API: GET /api/v1/items/{id}
- root.text 是 Show HN / Ask HN 的 post 正文
- root.children[] 是顶层评论(按 HN ranking 排序)
"""

import asyncio
from typing import Dict, List, Optional, Tuple

import aiohttp

from src.processor import html_to_markdown

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _is_internal_hn_url(url: str) -> bool:
    return url.startswith("https://news.ycombinator.com/item?id=")


async def _fetch_algolia_item(
    session: aiohttp.ClientSession, item_id: str, algolia_base: str, timeout: int
) -> Dict:
    url = f"{algolia_base}/items/{item_id}"
    async with session.get(
        url, timeout=aiohttp.ClientTimeout(total=timeout)
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Algolia /items/{item_id} 返回 {resp.status}")
        return await resp.json()


async def _fetch_url_html(
    session: aiohttp.ClientSession, url: str, timeout: int
) -> str:
    async with session.get(
        url, timeout=aiohttp.ClientTimeout(total=timeout)
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"外链 {url} 返回 {resp.status}")
        return await resp.text()


async def enrich_story(
    session: aiohttp.ClientSession,
    story: Dict,
    top_comments: int,
    comment_max_chars: int,
    link_content_max_chars: int,
    algolia_base: str,
    timeout: int,
) -> Dict:
    """对单 story enrich。任一子任务失败 → 对应字段留空,不抛。"""
    item_id = story["id"]
    is_internal = _is_internal_hn_url(story["url"])

    # 并发:Algolia + 外链(仅外链类)
    tasks = [_fetch_algolia_item(session, item_id, algolia_base, timeout)]
    if not is_internal:
        tasks.append(_fetch_url_html(session, story["url"], timeout))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    algolia_result = results[0]
    external_html_result = results[1] if not is_internal else None

    # 评论解析
    comments_list: List[str] = []
    post_text = ""
    if not isinstance(algolia_result, Exception) and algolia_result:
        post_text = algolia_result.get("text") or ""
        children = algolia_result.get("children") or []
        for child in children[:top_comments]:
            raw = (child or {}).get("text") or ""
            if not raw:
                continue
            md = html_to_markdown(raw)
            comments_list.append(md[:comment_max_chars])

    # link_content
    link_content = ""
    if is_internal:
        # Show HN / Ask HN:post 自身正文
        if post_text:
            link_content = html_to_markdown(post_text)[:link_content_max_chars]
    else:
        if not isinstance(external_html_result, Exception) and external_html_result:
            link_content = html_to_markdown(
                external_html_result, base_url=story["url"]
            )[:link_content_max_chars]

    return {
        **story,
        "link_content": link_content,
        "top_comments": comments_list,
    }


async def enrich_stories(
    stories: List[Dict],
    top_comments: int,
    comment_max_chars: int,
    link_content_max_chars: int,
    algolia_base: str = "https://hn.algolia.com/api/v1",
    timeout: int = 10,
) -> Tuple[List[Dict], List[str]]:
    """并发 enrich 多个 stories。"""
    errors: List[str] = []
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        results = await asyncio.gather(
            *[
                enrich_story(
                    session,
                    s,
                    top_comments,
                    comment_max_chars,
                    link_content_max_chars,
                    algolia_base,
                    timeout,
                )
                for s in stories
            ],
            return_exceptions=True,
        )
    enriched: List[Dict] = []
    for r, src in zip(results, stories):
        if isinstance(r, Exception):
            errors.append(f"enrich story {src['id']} 失败: {r}")
        else:
            enriched.append(r)
    return enriched, errors
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/pytest/test_sections_hackernews_enricher.py -v`
Expected: 4 pass.

- [ ] **Step 5: Commit**

```bash
git add src/sections/hackernews/item_enricher.py tests/pytest/test_sections_hackernews_enricher.py
git commit -m "feat(hn): add item enricher using Algolia + html_to_markdown"
```

---

### Task 15: Implement `src/sections/hackernews/section.py`

Glue: scrape → light LLM select → enrich → LLM summarize.

**Files:**
- Create: `src/sections/hackernews/section.py`
- Test: `tests/pytest/test_sections_hackernews_section.py`

- [ ] **Step 1: Write the failing test**

Create `tests/pytest/test_sections_hackernews_section.py`:

```python
import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.sections.hackernews.section import run_hackernews_section


def _cfg() -> dict:
    return {
        "filter": {"keep_days": 7},
        "sections": {
            "hackernews": {
                "enabled": True,
                "select_k": 1,
                "top_comments": 20,
                "comment_max_chars": 500,
                "link_content_max_chars": 3000,
                "request_timeout": 10,
                "algolia_base": "https://hn.algolia.com/api/v1",
            }
        },
        "llm": {
            "model": "x",
            "baseUrl": "http://x",
            "apiKeyName": "DEEPSEEK_API_KEY",
            "prompts": {
                "section_hackernews_select": "prompts/section_hackernews_select.md",
                "section_hackernews": "prompts/section_hackernews.md",
            },
        },
    }


@pytest.mark.asyncio
async def test_disabled_returns_empty():
    cfg = _cfg()
    cfg["sections"]["hackernews"]["enabled"] = False
    md, err = await run_hackernews_section(cfg, now=None)
    assert md == ""
    assert err is None


@pytest.mark.asyncio
async def test_select_empty_returns_silent():
    cfg = _cfg()
    with patch(
        "src.sections.hackernews.section.fetch_frontpage", new=AsyncMock(return_value="<html>")
    ), patch(
        "src.sections.hackernews.section.parse_frontpage_html",
        return_value=[{"id": "1", "title": "x"}],
    ), patch(
        "src.sections.hackernews.section.select_ai_related_hn",
        new=AsyncMock(return_value=([], None)),
    ):
        md, err = await run_hackernews_section(cfg, now=None)
    assert md == ""
    assert err is None


@pytest.mark.asyncio
async def test_happy_path():
    cfg = _cfg()
    front = [{"id": "1", "title": "AI thing", "url": "https://e.com/a", "site": "e.com", "points": 100, "comments": 5, "comments_url": "x"}]
    enriched = [{**front[0], "link_content": "body", "top_comments": ["c1"]}]

    with patch(
        "src.sections.hackernews.section.fetch_frontpage", new=AsyncMock(return_value="<html>")
    ), patch(
        "src.sections.hackernews.section.parse_frontpage_html", return_value=front
    ), patch(
        "src.sections.hackernews.section.select_ai_related_hn",
        new=AsyncMock(return_value=(["1"], None)),
    ), patch(
        "src.sections.hackernews.section.enrich_stories",
        new=AsyncMock(return_value=(enriched, [])),
    ), patch(
        "src.sections.hackernews.section.summarize_hackernews",
        new=AsyncMock(return_value=("## HN md", None)),
    ):
        md, err = await run_hackernews_section(cfg, now=None)
    assert md == "## HN md"
    assert err is None


@pytest.mark.asyncio
async def test_scrape_failure_returns_error():
    cfg = _cfg()
    with patch(
        "src.sections.hackernews.section.fetch_frontpage",
        new=AsyncMock(side_effect=RuntimeError("net")),
    ):
        md, err = await run_hackernews_section(cfg, now=None)
    assert md == ""
    assert "net" in err
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/pytest/test_sections_hackernews_section.py -v`
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement section**

Create `src/sections/hackernews/section.py`:

```python
"""HN 板块入口。流程:首页 → 轻 LLM 选 K → enrich → 最终 LLM 行文"""

from datetime import datetime
from typing import Dict, Optional, Tuple

from src.sections.hackernews.frontpage_scraper import (
    fetch_frontpage,
    parse_frontpage_html,
)
from src.sections.hackernews.item_enricher import enrich_stories


async def run_hackernews_section(
    config: Dict, now: Optional[datetime] = None
) -> Tuple[str, Optional[str]]:
    cfg = config.get("sections", {}).get("hackernews", {})
    if not cfg.get("enabled", False):
        return "", None

    from src.llm import select_ai_related_hn, summarize_hackernews

    timeout = cfg.get("request_timeout", 10)
    select_k = cfg.get("select_k", 1)
    top_comments = cfg.get("top_comments", 20)
    comment_max_chars = cfg.get("comment_max_chars", 500)
    link_content_max_chars = cfg.get("link_content_max_chars", 3000)
    algolia_base = cfg.get("algolia_base", "https://hn.algolia.com/api/v1")

    # 1. 抓首页
    try:
        html = await fetch_frontpage(timeout=timeout)
    except Exception as e:
        return "", f"HN 首页抓取失败: {e}"

    front = parse_frontpage_html(html)
    if not front:
        return "", None

    # 2. 轻 LLM 初筛
    selected_ids, select_err = await select_ai_related_hn(front, k=select_k, config=config["llm"])
    if select_err:
        return "", f"select_ai_related_hn: {select_err}"
    if not selected_ids:
        return "", None

    selected = [s for s in front if s["id"] in set(selected_ids)]
    if not selected:
        return "", None

    # 3. enrich
    enriched, enrich_errors = await enrich_stories(
        selected,
        top_comments=top_comments,
        comment_max_chars=comment_max_chars,
        link_content_max_chars=link_content_max_chars,
        algolia_base=algolia_base,
        timeout=timeout,
    )
    for e in enrich_errors:
        print(f"⚠️ HN enrich: {e}")
    if not enriched:
        return "", None

    # 4. LLM 总结
    md, err = await summarize_hackernews(enriched, config["llm"])
    if err:
        return "", f"summarize_hackernews: {err}"
    return md or "", None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/pytest/test_sections_hackernews_section.py -v`
Expected: 4 pass.

- [ ] **Step 5: Commit**

```bash
git add src/sections/hackernews/section.py tests/pytest/test_sections_hackernews_section.py
git commit -m "feat(hn): orchestrate frontpage → select → enrich → LLM"
```

---

### Task 16: Add `select_ai_related_hn` + `summarize_hackernews` to `src/llm.py` + 2 prompt files

**Files:**
- Modify: `src/llm.py`
- Create: `prompts/section_hackernews_select.md`
- Create: `prompts/section_hackernews.md`
- Test: `tests/pytest/test_llm_extra_sections.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/pytest/test_llm_extra_sections.py`:

```python
from llm import select_ai_related_hn, summarize_hackernews


@pytest.mark.asyncio
async def test_select_ai_related_hn_parses_id_array(tmp_path):
    prompt_path = tmp_path / "select.md"
    prompt_path.write_text("k={k} candidates={candidates_json}", encoding="utf-8")
    config = {
        "model": "x",
        "baseUrl": "http://x",
        "apiKeyName": "DEEPSEEK_API_KEY",
        "prompts": {"section_hackernews_select": str(prompt_path)},
    }
    with patch("llm.call_llm", new=AsyncMock(return_value='["111", "222"]')):
        ids, err = await select_ai_related_hn(
            [{"id": "111"}, {"id": "222"}, {"id": "333"}], k=2, config=config
        )
    assert ids == ["111", "222"]
    assert err is None


@pytest.mark.asyncio
async def test_select_ai_related_hn_empty_array(tmp_path):
    prompt_path = tmp_path / "select.md"
    prompt_path.write_text("{k}{candidates_json}", encoding="utf-8")
    config = {
        "model": "x",
        "baseUrl": "http://x",
        "apiKeyName": "DEEPSEEK_API_KEY",
        "prompts": {"section_hackernews_select": str(prompt_path)},
    }
    with patch("llm.call_llm", new=AsyncMock(return_value="[]")):
        ids, err = await select_ai_related_hn([{"id": "1"}], k=1, config=config)
    assert ids == []
    assert err is None


@pytest.mark.asyncio
async def test_summarize_hackernews_happy(tmp_path):
    prompt_path = tmp_path / "hn.md"
    prompt_path.write_text("{stories_json}", encoding="utf-8")
    config = {
        "model": "x",
        "baseUrl": "http://x",
        "apiKeyName": "DEEPSEEK_API_KEY",
        "prompts": {"section_hackernews": str(prompt_path)},
    }
    with patch("llm.call_llm", new=AsyncMock(return_value="## HN summary")):
        md, err = await summarize_hackernews(
            [{"id": "1", "title": "t", "link_content": "x", "top_comments": []}], config
        )
    assert md == "## HN summary"
    assert err is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/pytest/test_llm_extra_sections.py -v -k "select_ai_related_hn or summarize_hackernews"`
Expected: ImportError.

- [ ] **Step 3: Implement functions + prompts**

Append to `src/llm.py`:

```python
async def select_ai_related_hn(
    candidates: List[Dict], k: int, config: Dict
) -> Tuple[List[str], Optional[str]]:
    """轻 LLM:从 HN 首页候选元数据中挑 k 个 AI 相关 id。

    输入候选只含 id/title/site/points/comments 字段(不含正文)。
    """
    prompt_path = config.get("prompts", {}).get(
        "section_hackernews_select", "prompts/section_hackernews_select.md"
    )
    slim = [
        {
            "id": c.get("id"),
            "title": c.get("title", ""),
            "site": c.get("site", ""),
            "points": c.get("points", 0),
            "comments": c.get("comments", 0),
        }
        for c in candidates
    ]
    prompt = load_prompt(
        prompt_path,
        k=k,
        candidates_json=json.dumps(slim, ensure_ascii=False, indent=2),
    )
    try:
        response = await call_llm(prompt, config)
    except Exception as e:
        msg = f"select_ai_related_hn 失败: {e}"
        print(f"⚠️ {msg}")
        return [], msg

    try:
        ids = _parse_llm_json_response(response)
    except ValueError as e:
        msg = f"select_ai_related_hn 解析失败: {e}"
        print(f"⚠️ {msg}")
        return [], msg

    if not isinstance(ids, list):
        return [], "select_ai_related_hn 返回非数组"
    return [str(x) for x in ids][:k], None


async def summarize_hackernews(
    enriched_stories: List[Dict], config: Dict
) -> Tuple[str, Optional[str]]:
    """对输入的 K 个 enriched stories 行文。不传历史上下文。"""
    prompt_path = config.get("prompts", {}).get(
        "section_hackernews", "prompts/section_hackernews.md"
    )
    prompt = load_prompt(
        prompt_path,
        stories_json=json.dumps(enriched_stories, ensure_ascii=False, indent=2),
    )
    try:
        return await call_llm(prompt, config), None
    except Exception as e:
        msg = f"summarize_hackernews 失败: {e}"
        print(f"⚠️ {msg}")
        return "", msg
```

Create `prompts/section_hackernews_select.md`:

```markdown
你是 HN 早间选题人。从下列 30 条 HN 首页元数据中,挑出 **{k}** 个最符合关注领域的 story id。

## 关注领域(正面列表)
- AI Agent(智能体架构、工具链、多智能体、自主规划)
- AI 模型(训练、推理、微调、应用、语音/多模态)
- AI 基础设施(芯片、硬件、数据中心、推理优化、向量数据库、RAG 框架)
- 大厂/前沿动态(Apple、Google、Meta、OpenAI、Anthropic、Microsoft、xAI)
- AI 集成的开发者工具(API 网关、自动化、低代码与 AI 协同)

## 排除(负面列表)
- 嵌入式开发(Arduino、ESP32、树莓派、单片机)
- 底层系统编程(内存分配器、编译器、链接器,与 AI 无关时)
- 通用开发工具(命名规范、代码风格)
- 与 AI/科技无关的内容

## 决策原则
- title + site 不足以确定 AI 相关时,**宁可漏选不可错选**(错选会让最终板块写出与 AI Daily 调性无关的内容)
- 若全部 30 条都不符合,返回空数组 `[]`

## 输入
```json
{candidates_json}
```

## 输出
**只输出 JSON 数组**,如:`["12345", "67890"]` 或 `[]`
严禁输出任何解释性文字、markdown 包装、或自然语言句子。
```

Create `prompts/section_hackernews.md`:

```markdown
你是 HN 早间编辑。对输入的 enriched stories **全部行文**(不再二次挑选)。

## 输入(JSON 数组,字段含 link_content 与 top_comments)
```json
{stories_json}
```

## 内容要求(每条 story)
1. 提炼原文 `link_content` 的核心(背景 / 要点 / 结论)
2. 汇总 `top_comments` 中有价值的观点(支持 / 反对 / 补充),不是简单复述
3. 若评论中出现明显反驳原文的观点,必须保留并标注

## 输出格式(严格 Markdown,不要任何引导语)

```markdown
## 🟧 Hacker News 热议

### {{title}} ({{points}} pts · {{comments}} comments)

**📌 内容总结**

- 要点 1
- 要点 2
- 要点 3(可选)

**💬 HN 讨论**

- 观点 1(含反对/补充)
- 观点 2(可选)

🔗 [原文]({{url}}) | [HN 讨论页]({{comments_url}})
```

## 风格约束
- 客观、犀利、克制
- 避免与 RSS digest 句式雷同;不做行业宏大叙事
- 禁用词汇:震撼、炸裂、革命性、现象级
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/pytest/test_llm_extra_sections.py -v`
Expected: 5 tests pass (including the earlier GH tests).

- [ ] **Step 5: Commit**

```bash
git add src/llm.py prompts/section_hackernews_select.md prompts/section_hackernews.md tests/pytest/test_llm_extra_sections.py
git commit -m "feat(llm): add select_ai_related_hn and summarize_hackernews with prompts"
```

---

## Phase 5: Insights Module

### Task 17: Implement `insights/section.py` + `generate_trend_insights` + prompt

**Files:**
- Create: `src/sections/insights/__init__.py` (empty)
- Create: `src/sections/insights/section.py`
- Modify: `src/llm.py`
- Create: `prompts/insights.md`
- Test: `tests/pytest/test_sections_insights.py`

- [ ] **Step 1: Write the failing test**

Create `tests/pytest/test_sections_insights.py`:

```python
import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.sections.insights.section import run_insights_section


def _cfg() -> dict:
    return {
        "filter": {"push_context_days": 5},
        "sections": {"insights": {"enabled": True}},
        "llm": {
            "model": "x",
            "baseUrl": "http://x",
            "apiKeyName": "DEEPSEEK_API_KEY",
            "prompts": {"insights": "prompts/insights.md"},
        },
    }


@pytest.mark.asyncio
async def test_disabled_returns_empty():
    cfg = _cfg()
    cfg["sections"]["insights"]["enabled"] = False
    md, err = await run_insights_section("rss", "gh", "hn", cfg, now=None)
    assert md == ""
    assert err is None


@pytest.mark.asyncio
async def test_marks_empty_sections_for_llm():
    cfg = _cfg()
    captured = {}

    async def fake_gen(sections, recent_insights, config):
        captured["sections"] = sections
        return "insights md", None

    with patch(
        "src.sections.insights.section.load_recent_section_titles", return_value=""
    ), patch(
        "src.sections.insights.section.generate_trend_insights",
        new=AsyncMock(side_effect=fake_gen),
    ):
        md, err = await run_insights_section("", "gh md", "", cfg, now=None)

    assert md == "insights md"
    assert captured["sections"]["rss"] == "(本次无内容)"
    assert captured["sections"]["github"] == "gh md"
    assert captured["sections"]["hackernews"] == "(本次无内容)"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/pytest/test_sections_insights.py -v`
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement**

Create `src/sections/insights/__init__.py` (empty).

Create `src/sections/insights/section.py`:

```python
"""Insights 板块:基于 RSS/GH/HN 三段成品 + 近 N 天 insights 历史做跨板块小结"""

from datetime import datetime
from typing import Dict, Optional, Tuple

from src.storage import load_recent_section_titles

EMPTY_MARKER = "(本次无内容)"


async def run_insights_section(
    rss_md: str,
    gh_md: str,
    hn_md: str,
    config: Dict,
    now: Optional[datetime] = None,
) -> Tuple[str, Optional[str]]:
    cfg = config.get("sections", {}).get("insights", {})
    if not cfg.get("enabled", False):
        return "", None

    from src.llm import generate_trend_insights

    days = config["filter"].get("push_context_days", 5)
    recent = load_recent_section_titles("insights", days)

    sections = {
        "rss": rss_md or EMPTY_MARKER,
        "github": gh_md or EMPTY_MARKER,
        "hackernews": hn_md or EMPTY_MARKER,
    }

    md, err = await generate_trend_insights(sections, recent, config["llm"])
    if err:
        return "", f"generate_trend_insights: {err}"
    return md or "", None
```

Append to `src/llm.py`:

```python
async def generate_trend_insights(
    sections: Dict[str, str], recent_insights: str, config: Dict
) -> Tuple[str, Optional[str]]:
    """输入三段成品 + 近期 insights 标题清单,返回洞察段 markdown。"""
    prompt_path = config.get("prompts", {}).get("insights", "prompts/insights.md")
    prompt = load_prompt(
        prompt_path,
        rss=sections.get("rss", ""),
        github=sections.get("github", ""),
        hackernews=sections.get("hackernews", ""),
        recent_insights=recent_insights or "",
    )
    try:
        return await call_llm(prompt, config), None
    except Exception as e:
        msg = f"generate_trend_insights 失败: {e}"
        print(f"⚠️ {msg}")
        return "", msg
```

Create `prompts/insights.md`:

```markdown
你是 AI 行业观察员。基于今日三段已生成的成品做一段**跨板块趋势小结**。

## 今日素材(三段成品)

### RSS 板块
{rss}

### GitHub 板块
{github}

### Hacker News 板块
{hackernews}

## 近 N 天 insights 段标题(仅供风格参考与防趋同,严禁措辞模仿)
<RECENT BEGIN>
{recent_insights}
<RECENT END>

## 任务
基于今日三段成品,做跨板块小结。可参考的切入角度(不必全部覆盖,按今日素材最突出的张力来组织):
- 跨板块的"交叉信号"(同一话题在 RSS / GH / HN 中同时出现)
- 与近几天对比"新升温"或"退潮"的关键词
- "反直觉发现"(违反常识、值得停下来想一下的一条)
- 行业结构信号(资本 / 监管 / 算力 / 应用层等)

## 输出要求
- 直接输出 markdown 段,不带任何引导语
- 起始用 `## 💡 今日洞察`
- 总长度 200-400 字之间
- 不要简单复述其他板块的具体新闻;洞察的价值在"连接"而非"清单"
- 避免与 RSS digest 句式雷同;禁用宏大叙事词汇("深水区""临界点""下半场""博弈"等)
- 每条洞察须能溯源到今日素材中的具体信号,而不是凭空总结
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/pytest/test_sections_insights.py -v`
Expected: 2 pass.

- [ ] **Step 5: Commit**

```bash
git add src/sections/insights/ prompts/insights.md src/llm.py tests/pytest/test_sections_insights.py
git commit -m "feat(insights): add cross-section trend summary module"
```

---

## Phase 6: push_job Orchestration

### Task 18: Add `is_morning_push` to `src/main.py`

**Files:**
- Modify: `src/main.py`
- Test: `tests/pytest/test_morning_detection.py`

- [ ] **Step 1: Write the failing test**

Create `tests/pytest/test_morning_detection.py`:

```python
"""测试早报判定:cron + 容差"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.main import is_morning_push


TZ = timezone(timedelta(hours=8))


def _cfg(cron, tol=5):
    return {
        "schedule": {
            "morning_cron": cron,
            "morning_match_tolerance_minutes": tol,
            "timezone_hours": 8,
        }
    }


def test_returns_false_when_no_morning_cron_configured():
    assert is_morning_push(datetime(2026, 5, 17, 8, 0, tzinfo=TZ), {"schedule": {}}) is False


def test_match_exact_time():
    cfg = _cfg("0 8 * * *")
    now = datetime(2026, 5, 17, 8, 0, tzinfo=TZ)
    assert is_morning_push(now, cfg) is True


def test_match_within_tolerance():
    cfg = _cfg("0 8 * * *", tol=5)
    assert is_morning_push(datetime(2026, 5, 17, 8, 4, tzinfo=TZ), cfg) is True
    assert is_morning_push(datetime(2026, 5, 17, 7, 56, tzinfo=TZ), cfg) is True


def test_outside_tolerance():
    cfg = _cfg("0 8 * * *", tol=5)
    assert is_morning_push(datetime(2026, 5, 17, 8, 6, tzinfo=TZ), cfg) is False
    assert is_morning_push(datetime(2026, 5, 17, 9, 0, tzinfo=TZ), cfg) is False


def test_evening_time_not_morning():
    cfg = _cfg("0 8 * * *", tol=5)
    assert is_morning_push(datetime(2026, 5, 17, 17, 0, tzinfo=TZ), cfg) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/pytest/test_morning_detection.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement**

Open `src/main.py`. After the existing `calculate_push_times` function (around line 96), insert:

```python
def is_morning_push(now: datetime, config: Dict) -> bool:
    """判定当前时刻是否为早报触发点。

    用 cron + 容差判定,而非"今天的第一次推送":
    - 早报失败时,晚报不会错误升级为长版本
    - 容差直接绑定 cron 表达式,配置直观
    """
    morning_cron = config.get("schedule", {}).get("morning_cron")
    if not morning_cron:
        return False
    tolerance = timedelta(
        minutes=config["schedule"].get("morning_match_tolerance_minutes", 5)
    )
    base = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_fire = croniter(morning_cron, base).get_next(datetime)
    return abs(now - today_fire) <= tolerance
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/pytest/test_morning_detection.py -v`
Expected: 5 pass.

- [ ] **Step 5: Commit**

```bash
git add src/main.py tests/pytest/test_morning_detection.py
git commit -m "feat(main): add is_morning_push cron-based detector"
```

---

### Task 19: Add `_assemble_with_sentinels` helper

**Files:**
- Modify: `src/storage.py`
- Test: `tests/pytest/test_storage_sections.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/pytest/test_storage_sections.py`:

```python
from storage import assemble_with_sentinels


class TestAssembleWithSentinels:
    def test_assembles_all_sections_in_order(self):
        out = assemble_with_sentinels(
            {"rss": "R", "github": "G", "hackernews": "H", "insights": "I"}
        )
        # 顺序:rss → github → hackernews → insights
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/pytest/test_storage_sections.py::TestAssembleWithSentinels -v`
Expected: ImportError.

- [ ] **Step 3: Implement**

Append to `src/storage.py`:

```python
_SECTION_ORDER = ("rss", "github", "hackernews", "insights")


def assemble_with_sentinels(sections: Dict[str, str]) -> str:
    """按固定顺序拼装四段 markdown,每段包 sentinel;空段整段省略。"""
    parts: List[str] = []
    for key in _SECTION_ORDER:
        body = (sections.get(key) or "").strip()
        if not body:
            continue
        parts.append(f"<!-- SECTION:{key} BEGIN -->\n{body}\n<!-- SECTION:{key} END -->")
    return "\n\n".join(parts)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/pytest/test_storage_sections.py::TestAssembleWithSentinels -v`
Expected: 3 pass.

- [ ] **Step 5: Commit**

```bash
git add src/storage.py tests/pytest/test_storage_sections.py
git commit -m "feat(storage): add assemble_with_sentinels helper"
```

---

### Task 20: Refactor `run_push_job` to dispatch by morning detection

**Files:**
- Modify: `src/main.py`
- Test: `tests/pytest/test_main_run_push_job.py`

- [ ] **Step 1: Write the failing test**

Create `tests/pytest/test_main_run_push_job.py`:

```python
"""测试 run_push_job 的早报/默认路径分发"""

import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.main import run_push_job


@pytest.mark.asyncio
async def test_default_path_when_not_morning(sample_config):
    sample_config["schedule"]["morning_cron"] = "0 8 * * *"
    sample_config["schedule"]["morning_match_tolerance_minutes"] = 5
    sample_config["filter"]["push_context_days"] = 5

    with patch("src.main.is_morning_push", return_value=False), patch(
        "src.main._run_default_push", new=AsyncMock(return_value=None)
    ) as default_path, patch(
        "src.main._run_morning_push", new=AsyncMock(return_value=None)
    ) as morning_path:
        await run_push_job(sample_config)

    default_path.assert_awaited_once()
    morning_path.assert_not_awaited()


@pytest.mark.asyncio
async def test_morning_path_when_morning(sample_config):
    sample_config["schedule"]["morning_cron"] = "0 8 * * *"
    sample_config["schedule"]["morning_match_tolerance_minutes"] = 5
    sample_config["filter"]["push_context_days"] = 5

    with patch("src.main.is_morning_push", return_value=True), patch(
        "src.main._run_default_push", new=AsyncMock(return_value=None)
    ) as default_path, patch(
        "src.main._run_morning_push", new=AsyncMock(return_value=None)
    ) as morning_path:
        await run_push_job(sample_config)

    morning_path.assert_awaited_once()
    default_path.assert_not_awaited()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/pytest/test_main_run_push_job.py -v`
Expected: AttributeError or ImportError on `_run_default_push` / `_run_morning_push`.

- [ ] **Step 3: Refactor `run_push_job`**

Open `src/main.py`. Replace the entire `async def run_push_job(config: Dict):` function (currently around lines 280-333) with:

```python
async def run_push_job(config: Dict):
    print(f"\n{'=' * 50}")
    print(f"📤 Push Job | {now_local().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 50}")

    if is_morning_push(now_local(config), config):
        await _run_morning_push(config)
    else:
        await _run_default_push(config)


async def _run_default_push(config: Dict):
    """晚报或非早报时段:沿用原有纯 RSS digest 流程"""
    last_push_file = get_last_push_file()
    last_push_time = extract_push_time(last_push_file) if last_push_file else None
    if last_push_time:
        print(f"📌 上次推送: {last_push_time.strftime('%Y-%m-%d %H:%M')}")

    min_score = config["filter"]["min_score"]
    context_days = config["filter"]["context_days"]
    to_push, context = collect_entries_for_push(
        last_push_time=last_push_time,
        context_days=context_days,
        min_score=min_score,
    )
    print(
        f"📋 待推送 {len(to_push)} / 上下文 {len(context)} (≥{min_score} 分)"
    )
    if not to_push:
        print("ℹ️ 没有新消息需要推送")
        return

    push_context_days = config["filter"].get("push_context_days", 5)
    recent = load_recent_push_titles(push_context_days)

    print("🤖 生成推送内容...")
    try:
        push_content = await compose_digest(
            to_push, context, config["llm"], recent_push_context=recent
        )
    except Exception as e:
        print(f"生成汇总推送失败: {e}")
        await notify_llm_errors("compose_digest", [str(e)], config)
        raise

    await send_to_platforms(push_content, config["push"])
    push_file = get_push_file()
    save_push_file(push_file, push_content, len(to_push), len(to_push), profile="default")
    print(f"💾 已保存到 {push_file}")
    print(f"✅ Push Job 完成 | 推送: {len(to_push)} 条")
```

(Note: `_run_morning_push` is implemented in the next task. For this task to typecheck, also add this stub now — Task 21 fills in the body.)

After `_run_default_push`, add:

```python
async def _run_morning_push(config: Dict):
    """早报时段:四模块编排 + sentinel 拼装 (Task 21 will implement)"""
    raise NotImplementedError("Task 21 implements morning push")
```

Also add the imports the new code needs at the top of main.py — find the existing `from src.storage import (` block and add `assemble_with_sentinels,` to it. Also add the section imports (will be used in Task 21):

```python
from src.sections.github.section import run_github_section
from src.sections.hackernews.section import run_hackernews_section
from src.sections.insights.section import run_insights_section
from src.sections.rss.section import run_rss_section
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/pytest/test_main_run_push_job.py -v`
Expected: 2 pass.

- [ ] **Step 5: Commit**

```bash
git add src/main.py tests/pytest/test_main_run_push_job.py
git commit -m "refactor(main): split run_push_job into default + morning dispatchers"
```

---

### Task 21: Implement `_run_morning_push` four-module orchestrator

**Files:**
- Modify: `src/main.py` (replace the stub)
- Test: `tests/pytest/test_main_morning_push.py`

- [ ] **Step 1: Write the failing test**

Create `tests/pytest/test_main_morning_push.py`:

```python
"""测试早报四模块编排:gather + insights 串行 + sentinel 拼装 + 失败隔离"""

import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.main import _run_morning_push


@pytest.mark.asyncio
async def test_assembles_all_four_sections(sample_config):
    sample_config["filter"]["push_context_days"] = 5

    sent = {}
    async def fake_send(content, push_cfg):
        sent["content"] = content
    saved = {}
    def fake_save(filepath, content, source_count, total_entries, profile="default"):
        saved["profile"] = profile
        saved["content"] = content

    with patch("src.main.run_rss_section", new=AsyncMock(return_value=("R", None))), patch(
        "src.main.run_github_section", new=AsyncMock(return_value=("G", None))
    ), patch(
        "src.main.run_hackernews_section", new=AsyncMock(return_value=("H", None))
    ), patch(
        "src.main.run_insights_section", new=AsyncMock(return_value=("I", None))
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
    assert saved["profile"] == "morning"


@pytest.mark.asyncio
async def test_rss_failure_raises_to_caller(sample_config):
    sample_config["filter"]["push_context_days"] = 5

    with patch(
        "src.main.run_rss_section", new=AsyncMock(return_value=("", "compose_digest 失败"))
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
    async def fake_send(content, push_cfg):
        sent["content"] = content

    with patch("src.main.run_rss_section", new=AsyncMock(return_value=("R", None))), patch(
        "src.main.run_github_section", new=AsyncMock(return_value=("", "gh down"))
    ), patch(
        "src.main.run_hackernews_section", new=AsyncMock(return_value=("H", None))
    ), patch(
        "src.main.run_insights_section", new=AsyncMock(return_value=("I", None))
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/pytest/test_main_morning_push.py -v`
Expected: NotImplementedError (from the stub).

- [ ] **Step 3: Implement `_run_morning_push`**

In `src/main.py`, replace the stub:

```python
async def _run_morning_push(config: Dict):
    """早报四模块编排:RSS/GH/HN 并发 → insights 串行 → sentinel 拼装 → 推送 → 落盘。

    失败语义:
    - RSS 失败 → 整体抛 RuntimeError (核心承诺不变)
    - GH/HN/insights 失败 → 该段省略 + 告警,其他段照推
    """
    now = now_local(config)

    rss_result, gh_result, hn_result = await asyncio.gather(
        run_rss_section(config, now),
        run_github_section(config, now),
        run_hackernews_section(config, now),
    )

    rss_md, rss_err = rss_result
    gh_md, gh_err = gh_result
    hn_md, hn_err = hn_result

    # 失败告警(GH/HN 非阻塞)
    if gh_err:
        await notify_llm_errors("section_github", [gh_err], config)
    if hn_err:
        await notify_llm_errors("section_hackernews", [hn_err], config)

    # RSS 失败阻断
    if rss_err and not rss_md:
        await notify_llm_errors("compose_digest", [rss_err], config)
        raise RuntimeError(f"RSS section failed: {rss_err}")

    # insights(串行)
    insights_md, insights_err = await run_insights_section(
        rss_md, gh_md, hn_md, config, now
    )
    if insights_err:
        await notify_llm_errors("insights", [insights_err], config)

    final = assemble_with_sentinels(
        {
            "rss": rss_md,
            "github": gh_md,
            "hackernews": hn_md,
            "insights": insights_md,
        }
    )

    if not final.strip():
        print("ℹ️ 早报无任何段输出,跳过推送")
        return

    await send_to_platforms(final, config["push"])
    push_file = get_push_file()
    # source/total 在早报场景下意义弱化;沿用 RSS 段长度作为弱代理
    rss_count = rss_md.count("###") if rss_md else 0
    save_push_file(push_file, final, rss_count, rss_count, profile="morning")
    print(f"💾 已保存早报到 {push_file}")
```

Also ensure `asyncio` is imported at the top of `src/main.py` (it already is — line 4).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/pytest/test_main_morning_push.py tests/pytest/test_main_run_push_job.py -v`
Expected: all pass.

- [ ] **Step 5: Run all tests for regression check**

Run: `uv run pytest tests/pytest/ -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/main.py tests/pytest/test_main_morning_push.py
git commit -m "feat(main): implement four-module morning push orchestrator"
```

---

## Phase 7: Config, Docs, Integration Scripts

### Task 22: Update `config.json` and `config.json.example`

**Files:**
- Modify: `config.json`
- Modify: `config.json.example`

- [ ] **Step 1: Update `config.json`**

Open `config.json` and (a) add `"morning_cron"` and `"morning_match_tolerance_minutes"` under `schedule`, (b) add the entire `sections` block, (c) add the four new prompt paths under `llm.prompts`.

Insert under `schedule` (after `"push_cron"`):

```json
        "morning_cron": "0 8 * * *",
        "morning_match_tolerance_minutes": 5,
```

After the `schedule` block (or anywhere at top level), add:

```json
    "sections": {
        "github_trending": {
            "enabled": true,
            "max_items": 3,
            "max_deep_dive": 10,
            "readme_max_chars": 3000,
            "history_file": "news-data/trending-history.json",
            "request_timeout": 10,
            "tokenName": "GITHUB_TOKEN"
        },
        "hackernews": {
            "enabled": true,
            "select_k": 1,
            "top_comments": 20,
            "comment_max_chars": 500,
            "link_content_max_chars": 3000,
            "request_timeout": 10,
            "algolia_base": "https://hn.algolia.com/api/v1"
        },
        "insights": {
            "enabled": true
        }
    },
```

In the `llm.prompts` block, add:

```json
            "section_github": "prompts/section_github.md",
            "section_hackernews_select": "prompts/section_hackernews_select.md",
            "section_hackernews": "prompts/section_hackernews.md",
            "insights": "prompts/insights.md"
```

- [ ] **Step 2: Mirror the changes into `config.json.example`**

Apply the same edits to `config.json.example`.

- [ ] **Step 3: Validate JSON**

Run: `uv run python -c "import json; json.load(open('config.json')); json.load(open('config.json.example')); print('ok')"`
Expected: `ok`

- [ ] **Step 4: Confirm config loads cleanly**

Run: `uv run python -c "from src.config import load_config; c = load_config(); print(c['sections']['github_trending']['enabled'])"`
Expected: `True`

- [ ] **Step 5: Commit**

```bash
git add config.json config.json.example
git commit -m "feat(config): add sections + morning_cron schema"
```

---

### Task 23: Update `docs/tech-spec.md` (architecture sync)

**Files:**
- Modify: `docs/tech-spec.md`

- [ ] **Step 1: Add 板块编排 section**

Find the "## 关键模块边界" section in `docs/tech-spec.md`. After the existing `src/` tree block, add:

```markdown

### 板块化扩展 (morning push)

早报推送在 RSS 之上扩展三个板块:GitHub 趋势 / Hacker News 热议 / 跨板块洞察。模块结构、数据流与失败降级详见 `docs/extra-sections-design.md`。架构层关键约束:

- 仅在 `schedule.morning_cron` 命中(± `morning_match_tolerance_minutes`)时启用,晚报维持纯 RSS 行为
- 各板块封装为 `src/sections/<board>/section.py::run_xxx_section(config, now) -> (markdown, error)`
- `push_job` 用 `asyncio.gather` 并发跑 RSS / GH / HN,串行接 insights;最后用 `<!-- SECTION:xxx BEGIN/END -->` sentinel 包入 push 文件
- 仅 RSS 失败会让 push_job 整体退出非 0;其余板块失败 → 板块整段省略 + 告警

新增持久化文件:`news-data/trending-history.json`(GH 已查阅 repo 索引,按 `filter.keep_days` 过期)
```

- [ ] **Step 2: Update the data flow diagram (optional touch)**

In the same file's "### 数据流" section, leave the existing mermaid flowchart unchanged (it describes the RSS path); the new design doc carries the morning-specific flow.

- [ ] **Step 3: Update the maintenance date**

Find `> update: 2026-05-15` near the top and change to `> update: 2026-05-17`.

- [ ] **Step 4: Commit**

```bash
git add docs/tech-spec.md
git commit -m "docs(tech-spec): sync architecture with morning extra sections"
```

---

### Task 24: Update `README.md` config detail section

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Locate config detail section**

Open `README.md`. Find the section that documents `config.json` fields (typically headed "配置详解" or similar). Identify where `schedule` and `llm.prompts` are documented.

- [ ] **Step 2: Add `schedule.morning_cron` description**

Under `schedule` documentation, add:

```markdown
- `morning_cron` (可选): 早报 cron 表达式 (例: `"0 8 * * *"`)。命中时启用三个额外板块(GitHub / HN / 洞察)。缺失则全程纯 RSS。
- `morning_match_tolerance_minutes` (可选,默认 5): 早报判定容差(分钟)。
```

- [ ] **Step 3: Add `sections` block documentation**

Add a new subsection:

```markdown
### sections (早报扩展板块)

仅在 `schedule.morning_cron` 命中时生效。详细设计见 `docs/extra-sections-design.md`。

#### sections.github_trending
- `enabled`: 是否启用
- `max_items`: LLM 最终选出的项目数上限(默认 3)
- `max_deep_dive`: 单次最多 deep-dive 的候选 repo 数(默认 10)
- `readme_max_chars`: README 截断长度(默认 3000)
- `history_file`: trending 去重索引文件路径
- `request_timeout`: HTTP 超时秒
- `tokenName`: GitHub token 环境变量名;不设时匿名调用(限 60 req/hr)

#### sections.hackernews
- `enabled`: 是否启用
- `select_k`: 轻 LLM 从首页 30 条中挑出的故事数(默认 1)
- `top_comments`: 每个故事抓取的顶层评论数(默认 20)
- `comment_max_chars`: 单条评论截断长度(默认 500)
- `link_content_max_chars`: 外链正文截断长度(默认 3000)
- `request_timeout`: HTTP 超时秒
- `algolia_base`: Algolia API 基址

#### sections.insights
- `enabled`: 是否启用跨板块洞察段
```

- [ ] **Step 4: Add GITHUB_TOKEN to env vars table**

In the env vars section, add:

```markdown
- `GITHUB_TOKEN` (可选): GitHub API token,提升 deep-dive 限额到 5000 req/hr。不设时匿名调用(60 req/hr,日 20 calls 量级安全)。
```

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs(readme): document morning sections config + GITHUB_TOKEN"
```

---

### Task 25: Update `docs/plan.md`

**Files:**
- Modify: `docs/plan.md`

- [ ] **Step 1: Append decision + progress entries**

Open `docs/plan.md`. Find the `## 技术决策记录` heading. Append:

```markdown
### 2026-05-17: 早报扩展板块

- 决策:在 RSS digest 之上为早报增加 GitHub Trending / Hacker News / 跨板块洞察三段
- 触发条件:`schedule.morning_cron` 命中(± `morning_match_tolerance_minutes`)
- 模块边界:四个自治模块在 `src/sections/<board>/`,push_job 上游统一包 sentinel
- GH:单页 trending HTML 抓取 → history 去重 → REST API 拿 README+topics+metadata → LLM 选 1-3
- HN:首页 HTML → 轻 LLM 选 K(默认 1)→ Algolia API 拉评论 + html_to_markdown 拉外链 → LLM 行文
- 失败语义:RSS 失败整体退出;其余板块失败省略本段 + 告警
- 详细设计:`docs/extra-sections-design.md`;实施计划:`docs/superpowers/plans/2026-05-17-extra-sections.md`
```

Find `## 开发进度` heading. Append:

```markdown
### 2026-05-17

- ✅ 设计完成,详见 `docs/extra-sections-design.md`
- ✅ 实施计划完成,详见 `docs/superpowers/plans/2026-05-17-extra-sections.md`
- 🔄 实施中(按计划分 25 个任务推进)
```

- [ ] **Step 2: Commit**

```bash
git add docs/plan.md
git commit -m "docs(plan): record morning-sections decisions + progress"
```

---

### Task 26: Add manual test scripts

**Files:**
- Create: `tests/fetch_trending.py`
- Create: `tests/fetch_hackernews.py`
- Create: `tests/run_morning_push.py`

- [ ] **Step 1: Create `tests/fetch_trending.py`**

```python
"""手动跑一次 GH trending 抓取 + deep-dive,验证选择器与 API 接入"""

import asyncio
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import load_config
from src.sections.github.repo_enricher import enrich_repos
from src.sections.github.trending_scraper import (
    fetch_trending_page,
    parse_trending_html,
)


async def main():
    config = load_config()
    print("📥 抓取 GitHub Trending...")
    html = await fetch_trending_page(timeout=15)
    repos = parse_trending_html(html)
    print(f"📋 解析出 {len(repos)} 个 repo")
    for r in repos[:5]:
        print(f"  - {r['full_name']} ⭐{r['stars_today']}/{r['stars_total']} | {r['description'][:80]}")

    cfg = config["sections"]["github_trending"]
    print(f"\n🔍 enrich 前 {min(3, len(repos))} 个...")
    enriched, errors = await enrich_repos(
        repos[:3],
        token_env=cfg.get("tokenName", "GITHUB_TOKEN"),
        readme_max_chars=cfg.get("readme_max_chars", 3000),
        timeout=15,
    )
    for e in errors:
        print(f"  ⚠️ {e}")
    print(json.dumps(enriched, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Create `tests/fetch_hackernews.py`**

```python
"""手动跑一次 HN 首页 + Algolia enrich,验证选择器与 API"""

import asyncio
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.sections.hackernews.frontpage_scraper import (
    fetch_frontpage,
    parse_frontpage_html,
)
from src.sections.hackernews.item_enricher import enrich_stories


async def main():
    print("📥 抓取 HN 首页...")
    html = await fetch_frontpage(timeout=15)
    stories = parse_frontpage_html(html)
    print(f"📋 解析出 {len(stories)} 条")
    for s in stories[:5]:
        print(f"  - [{s['points']} pts · {s['comments']} comments] {s['title']} ({s['site']})")

    print("\n🔍 enrich 前 1 个外链类故事...")
    target = next((s for s in stories if not s["url"].startswith("https://news.ycombinator.com/")), stories[0])
    enriched, errors = await enrich_stories(
        [target],
        top_comments=5,
        comment_max_chars=300,
        link_content_max_chars=1500,
        timeout=15,
    )
    for e in errors:
        print(f"  ⚠️ {e}")
    print(json.dumps(enriched, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 3: Create `tests/run_morning_push.py`**

```python
"""模拟一次完整早报推送(强制 is_morning=True,但不发送到推送渠道)"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import load_config
from src.main import _run_morning_push


async def main():
    config = load_config()
    # 拦截真实推送,改为打印
    async def fake_send(content, push_cfg):
        print("\n" + "=" * 60)
        print("📤 假推送内容(实际不会发送)")
        print("=" * 60)
        print(content)

    with patch("src.main.send_to_platforms", new=AsyncMock(side_effect=fake_send)):
        await _run_morning_push(config)


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 4: Verify scripts at least parse**

Run: `uv run python -c "import ast; [ast.parse(open(f).read()) for f in ['tests/fetch_trending.py', 'tests/fetch_hackernews.py', 'tests/run_morning_push.py']]; print('ok')"`
Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add tests/fetch_trending.py tests/fetch_hackernews.py tests/run_morning_push.py
git commit -m "test: add interactive scripts for trending/HN/morning push smoke tests"
```

---

### Task 27: End-to-end smoke test with real APIs

This is a manual verification step before declaring the feature done.

- [ ] **Step 1: Run GH trending smoke test**

Run: `uv run python tests/fetch_trending.py`
Expected: prints 25+ repos, enriches 3, shows topics/readme_excerpt in JSON output.

- [ ] **Step 2: Run HN smoke test**

Run: `uv run python tests/fetch_hackernews.py`
Expected: prints 30 stories with points/comments, enriches 1 with link_content + top_comments.

- [ ] **Step 3: Run full morning push simulation**

Run: `uv run python tests/run_morning_push.py`
Expected: terminal prints the assembled push markdown containing 4 sections (or fewer with sentinel-bound omissions for any failing section). RSS section must be present (else the run aborts).

- [ ] **Step 4: Run full pytest suite**

Run: `uv run pytest tests/pytest/ -v`
Expected: all pass (including pre-existing tests).

- [ ] **Step 5: Commit nothing, but update plan.md progress**

Open `docs/plan.md` and update the most recent entry under `### 2026-05-17` from `🔄 实施中` to `✅ 实施完成`.

```bash
git add docs/plan.md
git commit -m "docs(plan): mark morning-sections implementation complete"
```

---

## Self-Review

**Spec coverage:**
- §2 architecture (4 modules + push_job orchestration) → Tasks 6, 10, 15, 17, 20, 21 ✓
- §3 module structure (`src/sections/...`) → created across Tasks 6, 8, 10, 13, 15, 17 ✓
- §4.1 sentinel contract → Task 19 (`assemble_with_sentinels`) ✓
- §4.2 `extract_section` + `load_recent_section_titles` → Tasks 1, 2 ✓
- §4.3 trending-history → Tasks 3, 5 ✓
- §5.1 RSS migration → Task 6 ✓
- §5.2 GitHub module flow → Tasks 7-11 ✓
- §5.3 Hacker News module flow → Tasks 12-16 ✓
- §5.4 Insights module → Task 17 ✓
- §6.1 LLM functions → Tasks 11, 16, 17 ✓
- §6.2 关注领域 + §6.3 prompts → Tasks 11, 16, 17 (prompts created with focus domain inline) ✓
- §6.4 调用顺序 → Task 21 ✓
- §7 insights formatting deferred to prompt → Task 17 prompt body honors this ✓
- §8 config schema → Task 22 ✓
- §9 failure isolation → Tasks 10, 15, 17, 21 ✓
- §10 morning detection → Task 18 ✓
- §11 integration points → Tasks 4 (save_push_file profile), 5 (cleanup history), 20 (main.py refactor) ✓
- §12 test strategy → Tasks 1-21 (unit), Task 26 (interactive scripts) ✓
- §13 implementation steps → matched 1:1 ✓

**Placeholder scan:** No TBDs; every code step has runnable code. Manual smoke tests in Task 27 are unavoidable manual verifications, called out explicitly.

**Type consistency:**
- `TrendingHistory.touch(url, today)` — signature is consistent across Task 3, Task 5, Task 10.
- `run_<board>_section(config, now) -> (str, Optional[str])` — consistent across Tasks 6, 10, 15.
- `run_insights_section(rss_md, gh_md, hn_md, config, now) -> (str, Optional[str])` — consistent across Task 17 + Task 21.
- `assemble_with_sentinels(dict[str, str]) -> str` — consistent in Task 19 definition + Task 21 use.
- `save_push_file(filepath, content, source_count, total_entries, profile="default")` — consistent in Task 4 definition + Task 20 default-path call + Task 21 morning-path call.

No issues found.
