"""Digest GitHub section regression tests (§5.4).

GitHub Trending lives outside the signal pool: the digest selects from the
``github_cache`` snapshot, suppresses repos already pushed inside the sliding
window (``github_push``), and records push history only after the digest was
actually sent — a repo that merely sat in the pool is never suppressed.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.db.bootstrap import run_migrations
from src.db.models import GithubPush, Signal, Watchlist
from src.db.repositories import set_github_cache
from src.db.session import dispose_engine, get_engine, session_scope
from src.push import digest as digest_module
from src.push.digest import build_digest, push_digest


# The score floor is pinned so these tests exercise the asset ledger rather
# than the default push threshold, which moves independently.
CONFIG = {
    "mode": "master",
    "output_language": "en",
    "delivery": {"push_limits": {"github": 2, "min_score": 60}},
}


def repo(full_name: str, *, url: str | None = None) -> dict:
    return {
        "full_name": full_name,
        "url": url or f"https://github.com/{full_name}",
        "description": f"{full_name} does things",
        "stars": 100,
    }


@pytest.fixture
def db(tmp_path):
    dispose_engine()
    database_url = f"sqlite:///{tmp_path / 'push_digest.db'}"
    run_migrations(database_url)
    yield get_engine(database_url, "news_agent")
    dispose_engine()


async def test_push_digest_records_push_history_after_send(db, monkeypatch):
    sent = []

    async def fake_send(markdown, platforms, *, title=""):
        sent.append(markdown)

    monkeypatch.setattr(digest_module, "send_to_platforms", fake_send)

    with session_scope() as session:
        set_github_cache(session, [repo("o/alpha"), repo("o/beta")])

    with session_scope() as session:
        digest = await push_digest(session, CONFIG)

    assert sent  # the digest went out
    assert [g["title"] for g in digest["github"]] == ["o/alpha", "o/beta"]
    with session_scope() as session:
        assert {row.url for row in session.query(GithubPush).all()} == {
            "https://github.com/o/alpha",
            "https://github.com/o/beta",
        }

    # A second push inside the window carries no GitHub section at all.
    with session_scope() as session:
        digest2 = await push_digest(session, CONFIG)
    assert digest2["github"] == []


async def test_hidden_signal_never_reaches_the_digest(db):
    stamp = datetime.now(timezone.utc)
    with session_scope() as session:
        session.add(
            Signal(
                id="hidden-high-final",
                url="https://news.example/hidden",
                url_hash="hash-hidden",
                batch_id="b1",
                title="Hidden high final score",
                summary="must never be pushed",
                source_name="rss",
                assets=["NVDA"],
                score=0,
                final_score=100,
                published_at=stamp,
                first_seen_at=stamp,
            )
        )

    with session_scope() as session:
        digest = build_digest(session, CONFIG)

    assert digest["hot"] == []
    assert digest["watchlist"] == []
    assert digest["github"] == []


# --------------------------------------------------------------------------- signal sections


def signal(signal_id: str, *, title: str, assets: list[str], score: float = 50.0) -> Signal:
    """A pool row with the fields the digest selection reads."""
    stamp = datetime.now(timezone.utc)
    return Signal(
        id=signal_id,
        url=f"https://news.example/{signal_id}",
        url_hash=f"hash-{signal_id}",
        batch_id="b1",
        title=title,
        summary=f"summary of {signal_id}",
        source_name="rss",
        assets=assets,
        score=80,
        final_score=score,
        published_at=stamp,
        first_seen_at=stamp,
    )


async def test_hot_section_keeps_one_signal_per_asset(db):
    """Five Bitcoin stories are one story: the best-scoring one wins (§5.3)."""
    with session_scope() as session:
        session.add_all(
            [
                signal("s1", title="Binance hack", assets=["BTC"], score=90),
                signal("s2", title="Binance hack again", assets=["BTC"], score=80),
                signal("s3", title="Nvidia guidance", assets=["NVDA"], score=70),
            ]
        )

    with session_scope() as session:
        digest = build_digest(session, CONFIG)

    assert [item["id"] for item in digest["hot"]] == ["s1", "s3"]


async def test_multi_asset_signal_is_rejected_when_any_asset_is_spent(db):
    """A partially overlapping multi-asset signal must not slip through."""
    with session_scope() as session:
        session.add_all(
            [
                signal("s1", title="Bitcoin and ETH outlook", assets=["BTC", "ETH"], score=90),
                signal("s2", title="Bitcoin market update", assets=["BTC"], score=80),
                signal("s3", title="Nvidia guidance", assets=["NVDA"], score=70),
            ]
        )

    with session_scope() as session:
        digest = build_digest(session, CONFIG)

    assert [item["id"] for item in digest["hot"]] == ["s1", "s3"]


async def test_topic_quota_keeps_digest_diverse(db):
    """One sector cannot fill every hot slot."""
    rows = []
    for index in range(5):
        row = signal(
            f"crypto-{index}",
            title=f"币安季度安全审查报告{index}",
            assets=[],
            score=99 - index,
        )
        row.keywords = [f"安全审查报告{index}", "风险披露", "合规"]
        row.sectors = ["加密货币"]
        rows.append(row)
    for index in range(3):
        row = signal(
            f"macro-{index}",
            title=f"美国非农就业报告{index}",
            assets=[],
            score=95 - index,
        )
        row.keywords = [f"通胀数据{index}", "就业", "利率"]
        row.sectors = ["宏观经济"]
        rows.append(row)

    with session_scope() as session:
        session.add_all(rows)

    with session_scope() as session:
        digest = build_digest(
            session,
            {
                **CONFIG,
                "delivery": {
                    "push_limits": {
                        "hot": 5,
                        "watchlist": 0,
                        "min_score": 60,
                        "max_per_topic": 3,
                    }
                },
            },
        )

    selected = digest["hot"]
    assert len(selected) == 5
    assert sum(item["sectors"][0] == "加密货币" for item in selected) <= 3
    assert sum(item["sectors"][0] == "宏观经济" for item in selected) <= 3


async def test_already_pushed_signals_are_not_pushed_again(db):
    """The morning digest's rows must not return in the evening one (§5.3)."""
    with session_scope() as session:
        session.add(signal("s1", title="Binance hack", assets=["BTC"], score=90))

    with session_scope() as session:
        first = await push_digest(session, CONFIG)
    assert [item["id"] for item in first["hot"]] == ["s1"]

    with session_scope() as session:
        second = await push_digest(session, CONFIG)
    assert second["hot"] == []


async def test_an_asset_spent_by_the_hot_section_cannot_reappear_in_watchlist(db):
    """The asset ledger is shared: one asset, one slot in the whole digest."""
    with session_scope() as session:
        session.add_all(
            [
                signal("s1", title="Binance hack", assets=["BTC"], score=90),
                signal("s2", title="Binance follow-up", assets=["BTC"], score=80),
                signal("s3", title="Nvidia guidance", assets=["NVDA"], score=70),
            ]
        )
        session.add(Watchlist(id="w1", owner="public", asset="BTC", asset_class="crypto"))

    with session_scope() as session:
        digest = build_digest(session, CONFIG)

    # The hot section spent BTC and NVDA, so the watchlist section has
    # nothing left to say about either.
    assert [item["id"] for item in digest["hot"]] == ["s1", "s3"]
    assert digest["watchlist"] == []


async def test_the_default_push_floor_keeps_only_the_top_signals(db):
    """The default floor is 80: a digest carries signals, not leftovers (§5.3).

    Below the floor a signal never reaches the reader at all — it is stored
    but not pushed, so a 70-point item is not worth a push slot.
    """
    with session_scope() as session:
        session.add_all(
            [
                signal("s1", title="Genuine signal", assets=["BTC"], score=90),
                signal("s2", title="Middling signal", assets=["NVDA"], score=85),
                signal("s3", title="Just below the floor", assets=["GOLD"], score=75),
            ]
        )

    with session_scope() as session:
        digest = build_digest(
            session,
            {"mode": "master", "output_language": "en"},
        )

    assert [item["id"] for item in digest["hot"]] == ["s1", "s2"]


async def test_a_low_score_from_the_newest_batch_does_not_outrank_an_older_high_score(db):
    """The digest ranks by score, not by arrival (§5.3).

    ``recent_signals`` orders by batch first for feed pagination; a digest
    that reused that order would let a 45-point signal from the newest batch
    outrank a 100-point signal that arrived an hour earlier.
    """
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        older = signal("older", title="Older high score", assets=["BTC"], score=95)
        older.batch_id = "2026-10-30T010000Z-cycle"
        older.first_seen_at = now - timedelta(hours=2)
        newer = signal("newer", title="Newest low score", assets=[], score=45)
        newer.batch_id = "2026-10-31T010000Z-cycle"
        newer.first_seen_at = now - timedelta(minutes=5)
        session.add_all([older, newer])

    with session_scope() as session:
        digest = build_digest(session, CONFIG)

    assert [item["id"] for item in digest["hot"]] == ["older"]
