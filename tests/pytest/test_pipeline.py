"""Unified fetch pipeline regression tests (§5.1).

The orchestrator is the only path into the ``signal`` table, so these tests pin
the wiring rather than the collectors: RSS and adapter entries must merge into
one scoring call, sectors must come from the ``sector`` table, dedup must run
before insert, ``first_seen_at`` must be generated at write time, and only
master may refresh the GitHub cache.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.db.bootstrap import run_migrations
from src.db.models import DedupDuplicate, GithubCache, Sector, Signal
from src.db.repositories import count_rows, list_sources
from src.db.session import (
    create_backend_indexes,
    dispose_engine,
    get_engine,
    session_scope,
)
from src.fetcher import STATUS_OK, FetchOutcome
from src import pipeline


NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def config():
    return {
        "mode": "master",
        "filter": {"max_age_hours": 24},
        "fetch": {"max_workers": 2, "timeout": 5},
        "ranking": {"title_weight": 3.0, "k1": 1.5, "b": 0.75},
        "dedup": {
            "bm25_low_threshold": 8.0,
            "bm25_high_threshold": 20.0,
            "top_k": 10,
            "window_hours": 72,
            "same_source_bonus": 2.0,
        },
        "llm": {"model": "test", "max_prompt_chars": 10000},
    }


@pytest.fixture
def db(tmp_path):
    dispose_engine()
    database_url = f"sqlite:///{tmp_path / 'pipeline.db'}"
    run_migrations(database_url)
    engine = get_engine(database_url, "news_agent")
    create_backend_indexes(engine)
    with session_scope() as session:
        session.add_all(
            [
                Sector(
                    id="sec-ai",
                    name="AI技术",
                    keywords=["ai chip"],
                    enabled=True,
                    sort_order=1,
                ),
                Sector(
                    id="sec-macro",
                    name="宏观经济",
                    keywords=["inflation"],
                    enabled=True,
                    sort_order=2,
                ),
                Sector(
                    id="sec-off",
                    name="已停用板块",
                    keywords=["crypto"],
                    enabled=False,
                    sort_order=3,
                ),
            ]
        )
    yield engine
    dispose_engine()


def entry(
    link: str,
    *,
    title: str = "Nvidia raises datacenter guidance",
    content: str = "The company lifted full year outlook on accelerator demand.",
    source: str = "Reuters",
    published: datetime = NOW - timedelta(hours=1),
    **extra,
):
    return {
        "title": title,
        "link": link,
        "published": published,
        "source": source,
        "content": content,
        "summary": "",
        **extra,
    }


@pytest.fixture
def scored(monkeypatch):
    """Patch the scorer; records the sectors it was handed."""

    async def fake_score_batch(entries, llm_config, sectors=None, duplicate_titles=None):
        fake_score_batch.calls.append(list(entries))
        fake_score_batch.sectors = sectors
        fake_score_batch.duplicate_titles = duplicate_titles
        return (
            [
                {
                    **e,
                    "score": 82,
                    "summary": "summary",
                    "quality_score": 80,
                    "interest_score": 84,
                    "keywords": ["nvidia", "guidance"],
                    "assets": ["NVDA"],
                    "direction": "bullish",
                    "sectors": ["AI技术"],
                }
                for e in entries
            ],
            [],
        )

    fake_score_batch.calls = []
    fake_score_batch.sectors = None
    fake_score_batch.duplicate_titles = None
    monkeypatch.setattr(pipeline, "score_batch", fake_score_batch)
    return fake_score_batch


def stub_fetch(monkeypatch, *, rss_entries=(), adapter_entries=None):
    """Replace both collectors with deterministic entry lists."""

    async def fake_fetch_all_sources(sources, cutoff_time, **kwargs):
        return [
            FetchOutcome(source=s, status=STATUS_OK, entries=list(rss_entries))
            for s in sources
        ]

    async def fake_fetch_adapter_sources(sources, cutoff_time, session=None, *, is_master=True):
        return dict(adapter_entries or {})

    monkeypatch.setattr(pipeline, "fetch_all_sources", fake_fetch_all_sources)
    monkeypatch.setattr(pipeline, "fetch_adapter_sources", fake_fetch_adapter_sources)


def add_source(session, source_id: str, kind: str, **overrides):
    from src.db.models import FetchSource

    values = {
        "id": source_id,
        "owner": "public",
        "kind": kind,
        "key": source_id,
        "title": source_id,
        "xml_url": f"https://{source_id}.example/rss" if kind == "rss" else "",
        "enabled": True,
        "params": {},
    }
    values.update(overrides)
    session.add(FetchSource(**values))


# --------------------------------------------------------------------------- merge


async def test_rss_and_adapter_entries_merge_into_one_scoring_call(db, config, monkeypatch, scored):
    with session_scope() as session:
        add_source(session, "rss-a", "rss")
        add_source(session, "adapter-hn", "hackernews")

    stub_fetch(
        monkeypatch,
        rss_entries=[entry("https://reuters.com/a")],
        adapter_entries={
            "adapter-hn": [
                entry(
                    "https://news.ycombinator.com/item?id=1",
                    title="Show HN: a local inference runtime",
                    content="A new runtime for running models on a laptop.",
                    source="Hacker News",
                )
            ]
        },
    )

    with session_scope() as session:
        report = await pipeline.run_fetch_cycle(session, config, owner="public", is_master=True, now=NOW)

    assert report.entries_fetched == 2
    assert len(scored.calls) == 1
    assert {e["source"] for e in scored.calls[0]} == {"Reuters", "Hacker News"}
    assert report.inserted == 2
    assert count_rows_signal(db) == 2
    with session_scope() as session:
        assert {row.batch_id for row in session.query(Signal).all()} == {report.batch_id}


async def test_slaver_fetches_feed_after_rss_before_adapters(db, config, monkeypatch, scored):
    config["mode"] = "slaver"
    calls = []
    with session_scope() as session:
        add_source(session, "rss-a", "rss", owner="user:7")
        add_source(session, "feed-a", "feed", owner="user:7", params={"url": "https://feed.example/news"})
        add_source(session, "adapter-hn", "hackernews", owner="user:7")

    async def fake_rss(sources, cutoff_time, **kwargs):
        calls.append("rss")
        return [FetchOutcome(source=source, status=STATUS_OK, entries=[]) for source in sources]

    async def fake_feed(sources, cutoff_time, session=None, *, is_master=True):
        calls.append("feed")
        return {"feed-a": [entry("https://feed.example/story", source="Feed A")]}

    async def fake_adapters(sources, cutoff_time, session=None, *, is_master=True):
        calls.append("adapters")
        return {}

    monkeypatch.setattr(pipeline, "fetch_all_sources", fake_rss)
    monkeypatch.setattr(pipeline, "fetch_feed_sources", fake_feed)
    monkeypatch.setattr(pipeline, "fetch_adapter_sources", fake_adapters)

    with session_scope() as session:
        report = await pipeline.run_fetch_cycle(
            session, config, owner="user:7", is_master=False, now=NOW
        )

    assert calls == ["rss", "feed", "adapters"]
    assert report.entries_fetched == 1
    assert report.inserted == 1
    assert scored.calls[0][0]["source"] == "Feed A"


def count_rows_signal(engine) -> int:
    with session_scope() as session:
        return count_rows(session, Signal)


# --------------------------------------------------------------------------- gates


async def test_stale_and_empty_entries_are_dropped_before_scoring(db, config, monkeypatch, scored):
    with session_scope() as session:
        add_source(session, "rss-a", "rss")

    stub_fetch(
        monkeypatch,
        rss_entries=[
            entry("https://reuters.com/fresh"),
            entry("https://reuters.com/stale", published=NOW - timedelta(hours=30)),
            entry("https://reuters.com/blank", content=""),
            entry("not-a-url"),
        ],
    )

    with session_scope() as session:
        report = await pipeline.run_fetch_cycle(session, config, owner="public", is_master=True, now=NOW)

    assert report.entries_fetched == 4
    assert report.entries_scored == 1
    assert report.inserted == 1
    assert count_rows_signal(db) == 1


async def test_freshness_window_never_exceeds_24h(db, config, monkeypatch, scored):
    config["filter"]["max_age_hours"] = 72
    cutoff = pipeline.freshness_cutoff(config, NOW)
    assert cutoff == NOW - timedelta(hours=24)


# --------------------------------------------------------------------------- scoring


async def test_scoring_receives_only_enabled_sectors(db, config, monkeypatch, scored):
    with session_scope() as session:
        add_source(session, "rss-a", "rss")

    stub_fetch(monkeypatch, rss_entries=[entry("https://reuters.com/a")])

    with session_scope() as session:
        await pipeline.run_fetch_cycle(session, config, owner="public", is_master=True, now=NOW)

    names = [s["name"] for s in scored.sectors]
    assert names == ["AI技术", "宏观经济"]


# --------------------------------------------------------------------------- dedup


async def test_duplicate_url_is_not_inserted_twice(db, config, monkeypatch, scored):
    with session_scope() as session:
        add_source(session, "rss-a", "rss")

    stub_fetch(monkeypatch, rss_entries=[entry("https://reuters.com/a")])

    with session_scope() as session:
        first = await pipeline.run_fetch_cycle(session, config, owner="public", is_master=True, now=NOW)
    with session_scope() as session:
        second = await pipeline.run_fetch_cycle(session, config, owner="public", is_master=True, now=NOW)

    assert first.inserted == 1
    assert second.inserted == 0
    assert second.duplicates == 1
    assert count_rows_signal(db) == 1
    with session_scope() as session:
        assert count_rows(session, DedupDuplicate) == 1


async def test_duplicate_url_from_another_outlet_merges_source_urls(db, config, monkeypatch, scored):
    with session_scope() as session:
        add_source(session, "rss-a", "rss")

    stub_fetch(monkeypatch, rss_entries=[entry("https://reuters.com/a")])
    with session_scope() as session:
        await pipeline.run_fetch_cycle(session, config, owner="public", is_master=True, now=NOW)

    stub_fetch(
        monkeypatch,
        rss_entries=[entry("https://reuters.com/a?utm_source=other", source="Yahoo")],
    )
    with session_scope() as session:
        await pipeline.run_fetch_cycle(session, config, owner="public", is_master=True, now=NOW)

    with session_scope() as session:
        row = session.query(Signal).one()
        assert row.source_urls == ["https://reuters.com/a"]
        assert row.source_name == "Reuters"


# --------------------------------------------------------------------------- persistence


async def test_first_seen_at_is_generated_at_write_time(db, config, monkeypatch, scored):
    """§5.7 windows the slaver pull on ``first_seen_at``, so it must not be the
    source's ``published_at``."""
    with session_scope() as session:
        add_source(session, "rss-a", "rss")

    stub_fetch(
        monkeypatch,
        rss_entries=[entry("https://reuters.com/late", published=NOW - timedelta(hours=2))],
    )

    with session_scope() as session:
        await pipeline.run_fetch_cycle(session, config, owner="public", is_master=True, now=NOW)

    with session_scope() as session:
        row = session.query(Signal).one()
        assert row.first_seen_at == NOW
        assert row.published_at == NOW - timedelta(hours=2)


async def test_master_writes_origin_master_and_slaver_writes_its_owner(db, config, monkeypatch, scored):
    with session_scope() as session:
        add_source(session, "rss-a", "rss")
        add_source(session, "rss-user7", "rss", owner="user:7")

    stub_fetch(monkeypatch, rss_entries=[entry("https://reuters.com/a")])
    with session_scope() as session:
        await pipeline.run_fetch_cycle(session, config, owner="public", is_master=True, now=NOW)

    with session_scope() as session:
        assert session.query(Signal).one().origin == "master"

    stub_fetch(monkeypatch, rss_entries=[entry("https://reuters.com/b")])
    with session_scope() as session:
        await pipeline.run_fetch_cycle(session, config, owner="user:7", is_master=False, now=NOW)

    with session_scope() as session:
        origins = {row.url: row.origin for row in session.query(Signal).all()}
        assert origins["https://reuters.com/b"] == "slaver:7"


async def test_url_less_signals_use_bm25_and_skip_same_batch_duplicates(db, config, monkeypatch, scored):
    with session_scope() as session:
        add_source(session, "feed-no-url", "feed")

    now = datetime.now(timezone.utc)
    story = entry(
        "",
        title="Bitcoin ETF inflows rise as institutional demand accelerates",
        content="Spot bitcoin funds reported sustained institutional inflows.",
        published=now - timedelta(hours=1),
    )
    stub_fetch(monkeypatch, adapter_entries={"feed-no-url": [story, story]})
    dedup_config = {
        **config,
        "dedup": {
            **config["dedup"],
            "bm25_low_threshold": 0.0,
            "bm25_high_threshold": 0.01,
        },
    }
    with session_scope() as session:
        report = await pipeline.run_fetch_cycle(
            session, dedup_config, owner="public", is_master=True, now=now
        )

    assert report.inserted == 1
    assert report.duplicates == 1
    with session_scope() as session:
        row = session.query(Signal).one()
        assert row.url is None
        assert row.source_urls == []


# --------------------------------------------------------------------------- github cache


async def test_master_refreshes_github_cache(db, config, monkeypatch, scored):
    with session_scope() as session:
        add_source(session, "gh", "github_trending")

    stub_fetch(
        monkeypatch,
        adapter_entries={
            "gh": [
                entry(
                    "https://github.com/o/r",
                    title="owner/repo",
                    is_github=True,
                    github={"url": "https://github.com/o/r", "full_name": "o/r"},
                )
            ]
        },
    )

    with session_scope() as session:
        report = await pipeline.run_fetch_cycle(session, config, owner="public", is_master=True, now=NOW)

    assert report.github_cached == 1
    # The snapshot is LLM-scored: one scoring call carrying the repo entry.
    assert len(scored.calls) == 1
    assert [e["link"] for e in scored.calls[0]] == ["https://github.com/o/r"]
    with session_scope() as session:
        cache = session.get(GithubCache, "latest")
        assert cache.payload == [
            {
                "url": "https://github.com/o/r",
                "full_name": "o/r",
                "score": 82,
                "summary": "summary",
                "name": "owner/repo",
                "first_seen_at": "2026-10-02T12:00:00+00:00",
            }
        ]
        # GitHub bypasses the signal pool (§5.4): no dedup, no signal row.
        assert session.query(Signal).count() == 0


# --------------------------------------------------------------------------- resilience


async def test_one_failing_source_does_not_abort_the_cycle(db, config, monkeypatch, scored):
    with session_scope() as session:
        add_source(session, "rss-a", "rss")
        add_source(session, "rss-b", "rss")

    async def fake_fetch_all_sources(sources, cutoff_time, **kwargs):
        return [
            FetchOutcome(
                source=s,
                status="error" if s.id == "rss-b" else STATUS_OK,
                entries=[] if s.id == "rss-b" else [entry("https://reuters.com/a")],
                error="HTTP 500" if s.id == "rss-b" else "",
            )
            for s in sources
        ]

    async def fake_fetch_adapter_sources(sources, cutoff_time, session=None, *, is_master=True):
        return {}

    monkeypatch.setattr(pipeline, "fetch_all_sources", fake_fetch_all_sources)
    monkeypatch.setattr(pipeline, "fetch_adapter_sources", fake_fetch_adapter_sources)

    with session_scope() as session:
        report = await pipeline.run_fetch_cycle(session, config, owner="public", is_master=True, now=NOW)

    assert report.sources_failed == 1
    assert report.inserted == 1
    with session_scope() as session:
        failed = next(s for s in list_sources(session, "public") if s.id == "rss-b")
        assert failed.last_error_at is not None


async def test_low_value_entry_is_stored_as_a_hidden_signal(db, config, monkeypatch):
    """Readable but with no actionable increment stays out of the public pool."""

    async def fake_score_batch(entries, llm_config, sectors=None, duplicate_titles=None):
        return (
            [
                {
                    **entry,
                    "score": 55,
                    "summary": "某沙龙举办，多位嘉宾到场。",
                    "quality_score": 55,
                    "interest_score": 50,
                    "keywords": ["论坛"],
                    "assets": [],
                    "direction": "neutral",
                    "sectors": [],
                    "is_low_value": True,
                }
                for entry in entries
            ],
            [],
        )

    monkeypatch.setattr(pipeline, "score_batch", fake_score_batch)

    with session_scope() as session:
        add_source(session, "rss-a", "rss")

    stub_fetch(monkeypatch, rss_entries=[entry("https://example.com/low-value")])

    with session_scope() as session:
        report = await pipeline.run_fetch_cycle(session, config, owner="public", is_master=True, now=NOW)

    assert report.inserted == 0
    assert report.hidden == 1
    assert report.entries_dropped == 0
    with session_scope() as session:
        assert session.query(Signal).one().score == 0


async def test_a_signal_below_the_visibility_floor_is_hidden(db, config, monkeypatch):
    """A low-score row is stored for the reporter denominator, never shown."""
    config["filter"]["min_score"] = 60

    async def fake_score_batch(entries, llm_config, sectors=None, duplicate_titles=None):
        return (
            [
                {
                    **entry,
                    "score": 55,
                    "summary": "某小型工具发布新版本。",
                    "quality_score": 55,
                    "interest_score": 55,
                    "keywords": ["工具"],
                    "assets": [],
                    "direction": "neutral",
                    "sectors": [],
                }
                for entry in entries
            ],
            [],
        )

    monkeypatch.setattr(pipeline, "score_batch", fake_score_batch)

    with session_scope() as session:
        add_source(session, "rss-a", "rss")

    stub_fetch(monkeypatch, rss_entries=[entry("https://example.com/below-floor")])

    with session_scope() as session:
        report = await pipeline.run_fetch_cycle(session, config, owner="public", is_master=True, now=NOW)

    assert report.inserted == 0
    assert report.hidden == 1
    assert report.entries_dropped == 0
    with session_scope() as session:
        assert session.query(Signal).one().score == 55


async def test_a_signal_above_the_store_floor_reaches_the_pool(db, config, monkeypatch):
    """The floor drops the weak signals, not the strong ones (§5.1)."""
    config["filter"]["min_score"] = 60

    async def fake_score_batch(entries, llm_config, sectors=None, duplicate_titles=None):
        return (
            [
                {
                    **entry,
                    "score": 75,
                    "summary": "某公司上调全年指引。",
                    "quality_score": 75,
                    "interest_score": 75,
                    "keywords": ["指引"],
                    "assets": ["NVDA"],
                    "direction": "bullish",
                    "sectors": ["AI技术"],
                }
                for entry in entries
            ],
            [],
        )

    monkeypatch.setattr(pipeline, "score_batch", fake_score_batch)

    with session_scope() as session:
        add_source(session, "rss-a", "rss")

    stub_fetch(monkeypatch, rss_entries=[entry("https://example.com/above-floor")])

    with session_scope() as session:
        report = await pipeline.run_fetch_cycle(session, config, owner="public", is_master=True, now=NOW)

    assert report.inserted == 1
    assert report.entries_dropped == 0
    with session_scope() as session:
        assert session.query(Signal).count() == 1


