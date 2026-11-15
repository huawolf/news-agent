"""Database layer regression tests.

Covers §4 of docs/system-architecture.md:
- Alembic owns the schema, both backends migrate cleanly.
- Seed import is idempotent and insert-only: a source a user disabled must stay
  disabled across restarts.
- The energy ledger balance is the sum of deltas.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.db import models as _models  # noqa: F401
from src.db.models import FetchSource, Sector, Signal
from src.db.repositories import (
    count_rows,
    delete_source,
    enabled_sector_specs,
    get_sector,
    get_source,
    list_sources,
    recall_candidates,
)
from src.db.seed import run_seed, seed_fetch_sources
from src.db.session import (
    create_backend_indexes,
    dispose_engine,
    get_engine,
    refresh_sqlite_fts,
    session_scope,
)

EXPECTED_TABLES = {
    "signal",
    "signal_translation",
    "watchlist",
    "sector",
    "fetch_source",
    "sync_state",
    "app_state",
    "dedup_duplicate",
    "energy_ledger",
    "user_profile",
    "reporter_scoring_usage",
    "github_cache",
    "local_setting",
}


def _alembic_head() -> str:
    """The newest revision on disk, resolved through Alembic itself."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).parent.parent.parent
    script = ScriptDirectory.from_config(
        Config(str(root / "alembic.ini"))
    )
    return script.get_current_head()


@pytest.fixture
def temp_resources(tmp_path):
    """A miniature resources dir: 2 sectors, 3 RSS feeds, 1 adapter."""
    root = tmp_path / "resources"
    root.mkdir()

    (root / "sectors.seed.json").write_text(
        json.dumps(
            {
                "version": 7,
                "sectors": [
                    {"name": "AI技术", "keywords": ["ai chip"], "sort_order": 1},
                    {"name": "宏观经济", "keywords": ["inflation"], "sort_order": 2},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    (root / "rss.opml").write_text(
        """<?xml version="1.0"?>
<opml version="2.0"><body>
  <outline type="rss" title="Feed A" xmlUrl="https://a.example/rss"/>
  <outline type="rss" title="Feed B" xmlUrl="https://b.example/rss"/>
  <outline type="rss" title="Feed C" xmlUrl="https://c.example/rss"/>
  <outline title="Not an RSS feed"/>
</body></opml>
""",
        encoding="utf-8",
    )

    (root / "adapters.seed.json").write_text(
        json.dumps(
            {
                "adapters": [
                    {"kind": "hackernews", "key": "hackernews", "enabled": True, "params": {"limit": 30}},
                    {"kind": "github_trending", "key": "github_trending", "enabled": False, "params": {}},
                ]
            }
        ),
        encoding="utf-8",
    )
    (root / "feeds.seed.json").write_text(
        json.dumps({"version": 1, "feeds": [
            {"key": "fixed-feed", "title": "Fixed Feed", "url": "https://feed.example/news"}
        ]}),
        encoding="utf-8",
    )
    (root / "retired_sources.seed.json").write_text(
        json.dumps(
            {
                "version": 1,
                "rss": [
                    {
                        "title": "Feed B",
                        "xml_url": "https://b.example/rss",
                        "reason": "produces multi-event roundups",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def db(tmp_path, temp_resources):
    """A migrated + seeded SQLite instance, torn down after the test."""
    dispose_engine()
    database_url = f"sqlite:///{tmp_path / 'news.db'}"
    config = {"mode": "slaver", "database": {"sqlite_path": str(tmp_path / "news.db")}}

    from src.db.bootstrap import run_migrations

    run_migrations(database_url)
    engine = get_engine(database_url, "news_agent")
    create_backend_indexes(engine)
    with session_scope() as session:
        run_seed(session, resources_dir=temp_resources)

    yield config, engine, temp_resources

    dispose_engine()


# --------------------------------------------------------------------------- schema


def test_migration_creates_every_table(db):
    _, engine, _ = db
    names = set(inspect(engine).get_table_names())
    assert EXPECTED_TABLES <= names
    preference_columns = {
        column["name"] for column in inspect(engine).get_columns("user_preference")
    }
    assert {"user_id", "regions", "signal_settings", "updated_at"} <= preference_columns


def test_migration_is_idempotent(db):
    _, engine, _ = db
    from src.db.bootstrap import run_migrations

    run_migrations(str(engine.url))
    with session_scope() as session:
        assert count_rows(session, Signal) == 0


def test_nullable_signal_url_has_unique_index_and_allows_multiple_nulls(db):
    _, engine, _ = db
    indexes = {index["name"]: index for index in inspect(engine).get_indexes("signal")}
    assert bool(indexes["uq_signal_url"]["unique"])
    assert indexes["uq_signal_url"]["column_names"] == ["url"]
    assert "signal_url_key" not in indexes

    with session_scope() as session:
        session.add_all(
            [
                _make_signal(id="url-less-a", url=None, url_hash="url-less-hash-a"),
                _make_signal(id="url-less-b", url=None, url_hash="url-less-hash-b"),
                _make_signal(id="url-present", url="https://example.com/unique", url_hash="url-present-hash"),
            ]
        )
    with session_scope() as session:
        assert session.query(Signal).filter(Signal.url.is_(None)).count() == 2

    with pytest.raises(IntegrityError):
        with session_scope() as session:
            session.add(
                _make_signal(
                    id="duplicate-url",
                    url="https://example.com/unique",
                    url_hash="duplicate-url-hash",
                )
            )


# --------------------------------------------------------------------------- seed


def test_first_seed_imports_expected_rows(db):
    _, _, resources = db
    with session_scope() as session:
        assert count_rows(session, Sector) == 2
        rss = list_sources(session, "public", kinds=["rss"])
        assert len(rss) == 3
        adapters = {row.kind: row for row in list_sources(session, "public") if row.kind in {"hackernews", "github_trending"}}
        assert set(adapters) == {"hackernews", "github_trending"}
        # Adapter defaults come from the seed file, including "off by default".
        assert adapters["github_trending"].enabled is False
        assert adapters["hackernews"].params == {"limit": 30}
        feeds = list_sources(session, "public", kinds=["feed"])
        assert len(feeds) == 1
        assert feeds[0].params == {"url": "https://feed.example/news"}
        assert feeds[0].enabled is True


def test_second_seed_inserts_nothing(db):
    _, _, resources = db
    with session_scope() as session:
        stats = run_seed(session, resources_dir=resources)
    assert stats["sectors"] == 0
    assert stats["fetch_sources"] == {"rss": 0, "adapters": 0, "feeds": 0}


def test_seed_does_not_reenable_user_disabled_source(db):
    _, _, resources = db
    with session_scope() as session:
        source = list_sources(session, "public", kinds=["rss"])[0]
        source.enabled = False

    with session_scope() as session:
        run_seed(session, resources_dir=resources)
        assert get_source(session, "public", "rss", source_key(source)).enabled is False


def test_seed_does_not_reset_edited_params(db):
    _, _, resources = db
    with session_scope() as session:
        row = get_source(session, "public", "hackernews", "hackernews")
        row.params = {"limit": 5}

    with session_scope() as session:
        run_seed(session, resources_dir=resources)
        assert get_source(session, "public", "hackernews", "hackernews").params == {"limit": 5}


def test_missing_sector_file_aborts(db):
    _, _, resources = db
    (resources / "sectors.seed.json").unlink()
    with pytest.raises(FileNotFoundError):
        with session_scope() as session:
            run_seed(session, owner="user:5", resources_dir=resources)


def test_seed_is_scoped_per_owner(db):
    _, _, resources = db
    with session_scope() as session:
        run_seed(session, owner="user:42", resources_dir=resources)
        assert count_rows(session, Sector) == 2  # sectors are global
        assert len(list_sources(session, "user:42", kinds=["rss"])) == 3
        # A deleted source for one owner must not resurrect for another.
        row = list_sources(session, "user:42", kinds=["rss"])[0]
        delete_source(session, "user:42", "rss", row.key)

    with session_scope() as session:
        assert seed_fetch_sources(session, "user:99", resources)["rss"] == 3
        assert seed_fetch_sources(session, "user:99", resources)["rss"] == 0


def source_key(source: FetchSource) -> str:
    return source.key


# --------------------------------------------------------------------------- owner restore


# --------------------------------------------------------------------------- sectors


def test_enabled_sector_specs_follow_the_database(db):
    """评分板块来自 sector 表的 enabled 行，不是种子文件。"""
    with session_scope() as session:
        assert enabled_sector_specs(session) == [
            {"name": "AI技术", "keywords": ["ai chip"]},
            {"name": "宏观经济", "keywords": ["inflation"]},
        ]

        get_sector(session, "宏观经济").enabled = False

    with session_scope() as session:
        assert [s["name"] for s in enabled_sector_specs(session)] == ["AI技术"]


# --------------------------------------------------------------------------- signals


def _make_signal(**overrides):
    values = {
        "url": "https://example.com/a",
        "url_hash": "hash-a",
        "source_urls": [],
        "title": "Nvidia ships a new datacenter chip",
        "content": "Nvidia announced a new accelerator for datacenters.",
        "title_search": "nvidia datacenter chip",
        "content_search": "nvidia accelerator datacenters",
        "keywords": ["nvidia", "chip"],
        "source_name": "Example",
        "assets": ["NVDA"],
        "sectors": ["AI技术"],
        "direction": "bullish",
        "origin": "master",
        "score": 80,
        "batch_id": "batch-test",
    }
    values.update(overrides)
    return Signal(id=values.pop("id", "sig-1"), **values)


# --------------------------------------------------------------------------- energy


# --------------------------------------------------------------------------- sqlite fts


def test_sqlite_fts_recall_matches_tokens(db):
    with session_scope() as session:
        session.add(_make_signal(id="fts-1"))
        refresh_sqlite_fts(session, ["hash-a"])

    since = datetime.now(timezone.utc) - timedelta(days=1)
    with session_scope() as session:
        assert [row.id for row in recall_candidates(session, ["nvidia"], since)] == ["fts-1"]
        assert recall_candidates(session, ["bitcoin"], since) == []


