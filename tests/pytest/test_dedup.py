"""Dedup pipeline regression tests (§5.4).

Exercises URL and BM25 dedup and asserts the ``dedup_duplicate`` archive and
``source_urls`` merge behaviour.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.db.bootstrap import run_migrations
from src.db.models import DedupDuplicate, Signal
from src.db.repositories import count_rows
from src.db.session import (
    create_backend_indexes,
    dispose_engine,
    get_engine,
    refresh_sqlite_fts,
    session_scope,
)
from src.dedup import pipeline
from src.dedup.urls import canonical_url, url_hash


@pytest.fixture
def config():
    return {
        "mode": "slaver",
        "dedup": {
            "bm25_low_threshold": 8.0,
            "bm25_high_threshold": 20.0,
            "top_k": 10,
            "window_hours": 72,
            "same_source_bonus": 2.0,
        },
        "ranking": {"title_weight": 3.0, "k1": 1.5, "b": 0.75},
    }


@pytest.fixture
def db(tmp_path):
    dispose_engine()
    database_url = f"sqlite:///{tmp_path / 'dedup.db'}"
    run_migrations(database_url)
    engine = get_engine(database_url, "news_agent")
    create_backend_indexes(engine)
    yield engine
    dispose_engine()


def store(session, **overrides):
    """Insert one signal, pre-tokenising exactly like the pipeline does."""
    title = overrides.pop("title", "Nvidia announces new datacenter GPU")
    content = overrides.pop("content", "Nvidia said the accelerator ships in March.")
    assets = overrides.pop("assets", ["NVDA"])
    source_name = overrides.pop("source_name", "Reuters")
    signal_id = overrides.pop("id", "s1")
    from src.dedup.tokenizer import build_search_text

    url = overrides.pop("url", "https://example.com/nvidia-gpu")
    canonical = canonical_url(url)
    signal = Signal(
        id=signal_id,
        url=canonical,
        url_hash=url_hash(canonical),
        title=title,
        content=content,
        title_search=build_search_text(title, assets),
        content_search=build_search_text(content, assets),
        keywords=[],
        source_name=source_name,
        assets=assets,
        sectors=["AI技术"],
        direction="bullish",
        origin="master",
        score=80,
        batch_id="batch-test",
        **overrides,
    )
    session.add(signal)
    session.flush()
    refresh_sqlite_fts(session, [signal.url_hash])
    return signal


# BM25 idf is corpus relative: a term present in every document contributes
# almost nothing, so a one-document corpus makes every score collapse towards
# zero and the configured thresholds unreachable. The background corpus below
# keeps idf in the same range as production.
BACKGROUND = [
    ("Fed holds rates steady as inflation cools", "Policymakers signalled patience while price pressures ease.", ["DXY"], "Fed"),
    ("Treasury yields slip after weak payrolls", "The ten year note led a broad decline in government bonds.", ["US10Y"], "Bloomberg"),
    ("Crude retreats as inventories build", "Oil fell after official data showed a surprise build in crude stockpiles.", ["CRUDEOIL"], "Reuters"),
    ("Gold demand rises in India", "Jewellery buyers absorbed bullion ahead of the festival season.", ["GOLD"], "FT"),
    ("Copper smelters cut output guidance", "Treatment charges collapsed, signalling a tighter concentrate market.", ["COPPER"], "Reuters"),
    ("Bitcoin ETFs log fourth week of inflows", "Issuers absorbed fresh capital as the token held above its range.", ["BTC"], "CoinDesk"),
    ("Ethereum staking queue clears", "Validators exited en masse after the upgrade shipped on schedule.", ["ETH"], "CoinDesk"),
    ("Semiconductor equipment orders rebound", "Bookings in wafer fab tools turned up after a two year slump.", ["SOXX"], "Nikkei"),
    ("Cloud capex guidance raised again", "Hyperscalers lifted spending plans on accelerated compute demand.", ["CLOUD"], "WSJ"),
    ("Shipping rates slide on Red Sea normalisation", "Container spot rates retreated to pre-disruption levels.", ["BDI"], "Lloyd's List"),
    ("European gas prices ease on mild weather", "Storage levels stayed comfortable through the month.", ["TTF"], "Reuters"),
    ("Silver squeeze tightens physical market", "Lease rates spiked as inventories thinned in London vaults.", ["SILVER"], "FT"),
    ("Japanese yen strengthens after wage data", "The currency extended gains following a hot labour print.", ["USDJPY"], "Reuters"),
    ("China property starts fall again", "Developers started fewer projects as funding stayed tight.", ["XIN9"], "SCMP"),
    ("Lithium prices stabilise after mine cuts", "Supply curtailments finally balanced a lingering oversupply.", ["LIT"], "FT"),
]


def seed_background(session, count: int = 12) -> None:
    from src.dedup.tokenizer import build_search_text

    for index, (title, content, assets, source) in enumerate(BACKGROUND[:count]):
        url = f"https://background.example/story-{index}"
        canonical = canonical_url(url)
        session.add(
            Signal(
                id=f"bg-{index}",
                url=canonical,
                url_hash=url_hash(canonical),
                title_search=build_search_text(title, assets),
                content_search=build_search_text(content, assets),
                keywords=[],
                source_name=source,
                assets=assets,
                sectors=["宏观经济"],
                direction="neutral",
                origin="master",
                score=80,
                batch_id="batch-background",
                title=title,
                content=content,
            )
        )
    session.flush()
    refresh_sqlite_fts(session, [f"bg-{i}" for i in range(count)])


def candidate(url, title=None, content=None, source_name="Reuters"):
    return pipeline.prepare_candidate(
        {
            "url": url,
            "title": title if title is not None else "Nvidia announces new datacenter GPU",
            "content": content if content is not None else "Nvidia said the accelerator ships in March.",
            "source_name": source_name,
        }
    )


# --------------------------------------------------------------------------- level 1


async def test_url_level_hit_is_archived(db, config):
    with session_scope() as session:
        store(session, id="s1")

    with session_scope() as session:
        duplicate, decision = await pipeline.apply(session, config, candidate("https://example.com/nvidia-gpu?utm_source=x"))
        assert duplicate is True
        assert decision.match_level == pipeline.MATCH_URL
        assert decision.decided_by == pipeline.DECIDED_RULE
        assert count_rows(session, DedupDuplicate) == 1


async def test_url_level_matches_merged_source_urls(db, config):
    """A URL already merged into source_urls must still be caught."""
    with session_scope() as session:
        store(session, id="s1", source_urls=["https://other.example/story"])

    with session_scope() as session:
        decision = await pipeline.check(session, config, candidate("https://other.example/story"))
        assert decision.is_duplicate is True
        assert decision.match_level == pipeline.MATCH_URL


# --------------------------------------------------------------------------- level 2


async def test_high_bm25_score_archives_without_merge(db, config):
    """A BM25-level hit archives the candidate but never merges its URL.

    Merging on a probabilistic hit would permanently bury a false-kill article
    behind the survivor's URL scan, so only URL-level hits may merge.
    """
    with session_scope() as session:
        seed_background(session)
        survivor = store(session, id="s1")
        scored = pipeline.prepare_candidate(
            {
                "url": "https://other.example/nvidia-gpu-reprint",
                "title": survivor.title,
                "content": survivor.content,
                "source_name": "Reuters",
            }
        )
        duplicate, decision = await pipeline.apply(session, config, scored)
        assert duplicate is True
        assert decision.match_level == pipeline.MATCH_BM25
        assert "https://other.example/nvidia-gpu-reprint" not in survivor.source_urls
        assert count_rows(session, DedupDuplicate) == 1


# --------------------------------------------------------------------------- level 3


async def test_bm25_grey_zone_is_unique_without_jev(db, config):
    with session_scope() as session:
        seed_background(session)
        survivor = store(session, id="s1")
        # Same tokens as the stored signal but a very small body, so the score
        # lands between the two thresholds instead of above the high one.
        scored = pipeline.prepare_candidate(
            {
                "url": "https://other.example/reprint",
                "title": survivor.title,
                "content": survivor.title,
                "source_name": "Reuters",
            }
        )
        dedup = {**config["dedup"], "bm25_low_threshold": 0.01, "bm25_high_threshold": 10_000.0}
        duplicate, decision = await pipeline.apply(session, {**config, "dedup": dedup}, scored)

        assert duplicate is False
        assert decision.match_level == ""
        assert survivor.source_urls == []
        assert count_rows(session, DedupDuplicate) == 0


# --------------------------------------------------------------------------- misc


async def test_window_excludes_old_signals(db, config):
    """Signals older than the dedup window are not candidates."""
    with session_scope() as session:
        seed_background(session)
        old = store(session, id="old", title="Nvidia announces new datacenter GPU")
        old.first_seen_at = datetime.now(timezone.utc) - timedelta(hours=200)
        session.flush()
        refresh_sqlite_fts(session, [old.url_hash])

    with session_scope() as session:
        decision = await pipeline.check(session, config, candidate("https://example.com/fresh"))
    assert decision.is_duplicate is False


