"""Built-in adapter regression tests (§4.1).

The built-in collectors are ``fetch_source`` rows driven entirely by
``params``. What must not regress:

- the dispatcher routes on ``kind`` and rejects unknown kinds;
- a failing adapter never takes the cycle down, and stamps ``last_error_at``;
- ``enabled = false`` and master-only kinds are skipped where they must be;
- Google News cursors live in ``local_setting`` and only advance for feeds that
  actually succeeded;
- no entry carries the deleted ``category`` field.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src import adapters
from src.adapters import feed_browser
from src.adapters import google_news as gn
from src.adapters import signals as sig
from src.db.models import FetchSource


def make_source(**overrides) -> FetchSource:
    values = {
        "id": "src-1",
        "owner": "user:1",
        "kind": "signals",
        "key": "signals",
        "title": "Signals",
        "xml_url": "",
        "enabled": True,
        "params": {},
    }
    values.update(overrides)
    return FetchSource(**values)


@pytest.fixture
def cutoff():
    return datetime.now(timezone.utc) - timedelta(hours=24)


@pytest.fixture
def db(tmp_path):
    from src.db.bootstrap import run_migrations
    from src.db.session import (
        create_backend_indexes,
        dispose_engine,
        get_engine,
        session_scope,
    )

    dispose_engine()
    database_url = f"sqlite:///{tmp_path / 'adapters.db'}"
    run_migrations(database_url)
    create_backend_indexes(get_engine(database_url, "news_agent"))
    yield session_scope
    dispose_engine()


# --------------------------------------------------------------------------- routing


async def test_one_failing_adapter_does_not_cancel_the_others(cutoff, monkeypatch):
    async def boom(_source, _cutoff, _session):
        raise RuntimeError("upstream is down")

    async def fine(_source, _cutoff, _session):
        return [{"title": "ok", "link": "https://x/ok"}]

    monkeypatch.setitem(adapters._HANDLERS, "hackernews", boom)
    monkeypatch.setitem(adapters._HANDLERS, "signals", fine)

    rows = [make_source(id="hn-1", kind="hackernews"), make_source(id="sig-1", kind="signals")]
    collected = await adapters.fetch_adapter_sources(rows, cutoff)

    assert list(collected) == ["sig-1"]


async def test_disabled_rows_and_non_adapter_kinds_are_skipped(cutoff, monkeypatch):
    seen = []

    async def record(source, _cutoff, _session):
        seen.append(source.id)
        return []

    for kind in adapters.ADAPTER_KINDS:
        monkeypatch.setitem(adapters._HANDLERS, kind, record)

    rows = [
        make_source(id="on", kind="signals", enabled=True),
        make_source(id="off", kind="signals", enabled=False),
        make_source(id="rss", kind="rss", enabled=True),
    ]

    assert await adapters.fetch_adapter_sources(rows, cutoff) == {"on": []}
    assert seen == ["on"]


def test_feed_source_uses_private_browserworker_session_and_maps_items(cutoff, monkeypatch):
    calls = {}
    source = make_source(
        id="feed-1",
        kind="feed",
        key="cryptopanic-home",
        title="CryptoPanic",
        owner="user:7",
        params={"url": "https://cryptopanic.com/"},
    )

    class FakeCrawler:
        def __init__(self, **kwargs):
            calls["crawler_kwargs"] = kwargs
            self.cdp = type("CDP", (), {"daemon_port": 12345})()

        def crawl_url(self, url, **kwargs):
            calls["crawl"] = (url, kwargs)
            return {
                "success": True,
                "items": [
                    {
                        "title": "Bitcoin ETF sees fresh inflows",
                        "content": "Spot funds recorded another day of inflows.",
                        "url": "https://cryptopanic.com/news/12345",
                        "published_at": 1_800_000_000,
                    }
                ],
            }

    monkeypatch.setattr(feed_browser, "CryptoCrawler", FakeCrawler)
    monkeypatch.setattr(feed_browser, "_acquire_browser_lock", lambda sid, domains, port: calls.update(lock=(sid, domains, port)))
    monkeypatch.setattr(feed_browser, "_release_browser_lock", lambda sid, port: calls.update(release=(sid, port)))

    entries = feed_browser.fetch_feed_source(source, cutoff)

    assert calls["crawler_kwargs"]["persist_store"] is False
    assert calls["crawler_kwargs"]["session_id"].startswith("news_agent_feed_")
    assert calls["crawl"][0] == "https://cryptopanic.com/"
    assert calls["crawl"][1]["persist"] is False
    assert calls["crawl"][1]["since_time"] == cutoff
    assert calls["lock"][1:] == (["cryptopanic.com"], 12345)
    assert calls["release"][1] == 12345
    assert entries == [
        {
            "title": "Bitcoin ETF sees fresh inflows",
            "link": "https://cryptopanic.com/news/12345",
            "published": datetime.fromtimestamp(1_800_000_000, timezone.utc),
            "source": "CryptoPanic",
            "content": "Spot funds recorded another day of inflows.",
            "summary": "Spot funds recorded another day of inflows.",
        }
    ]


async def test_slaver_feed_sources_run_sequentially_and_normalize_entries(cutoff, db, monkeypatch):
    calls = []
    feeds = [
        make_source(id="feed-a", kind="feed", key="a", title="Feed A", params={"url": "https://a.example/news"}),
        make_source(id="feed-b", kind="feed", key="b", title="Feed B", params={"url": "https://b.example/news"}),
    ]

    def fake_fetch(source, _cutoff):
        calls.append(source.id)
        return [{
            "title": source.title,
            "link": f"https://{source.key}.example/story",
            "published": datetime.fromtimestamp(1_800_000_000, timezone.utc),
            "source": source.title,
            "content": "story",
            "summary": "story",
        }]

    monkeypatch.setattr(feed_browser, "fetch_feed_source", fake_fetch)
    with db() as session:
        session.add_all(feeds)
    with db() as session:
        collected = await feed_browser.fetch_feed_sources(feeds, cutoff, session, is_master=False)

    assert calls == ["feed-a", "feed-b"]
    assert collected["feed-a"][0]["link"] == "https://a.example/story"
    assert collected["feed-a"][0]["published"].tzinfo == timezone.utc
    with db() as session:
        rows = {row.id: row for row in session.query(FetchSource).all()}
    assert rows["feed-a"].last_fetched_at is not None
    assert rows["feed-b"].last_fetched_at is not None


async def test_feed_fetch_uses_last_successful_fetch_as_since(cutoff, db, monkeypatch):
    previous_fetch = cutoff + timedelta(hours=4)
    feed = make_source(
        id="feed-cursor",
        kind="feed",
        key="cursor",
        params={"url": "https://feed.example/news"},
        last_fetched_at=previous_fetch,
    )
    captured = []

    def fake_fetch(source, since_time):
        captured.append(since_time)
        return []

    monkeypatch.setattr(feed_browser, "fetch_feed_source", fake_fetch)
    with db() as session:
        session.add(feed)
    with db() as session:
        await feed_browser.fetch_feed_sources([feed], cutoff, session, is_master=False)

    assert captured == [previous_fetch]


async def test_failed_feed_is_logged_and_stamped_without_blocking_next_source(cutoff, db, monkeypatch):
    previous_fetch = cutoff + timedelta(hours=2)
    feeds = [
        make_source(id="feed-bad", kind="feed", key="bad", params={"url": "https://bad.example/news"}, last_fetched_at=previous_fetch),
        make_source(id="feed-good", kind="feed", key="good", params={"url": "https://good.example/news"}),
    ]

    def fake_fetch(source, _cutoff):
        if source.id == "feed-bad":
            raise RuntimeError("daemon unavailable")
        return []

    monkeypatch.setattr(feed_browser, "fetch_feed_source", fake_fetch)
    with db() as session:
        session.add_all(feeds)
    with db() as session:
        collected = await feed_browser.fetch_feed_sources(feeds, cutoff, session, is_master=False)

    assert collected == {"feed-good": []}
    with db() as session:
        rows = {row.id: row for row in session.query(FetchSource).all()}
    assert rows["feed-bad"].last_error_at is not None
    assert rows["feed-bad"].last_fetched_at == previous_fetch
    assert rows["feed-good"].last_fetched_at is not None


# --------------------------------------------------------------------------- status stamps


# --------------------------------------------------------------------------- google news


def _rss(items):
    body = "".join(
        f"""
        <item>
          <title>{title}</title>
          <link>https://example.com/{index}</link>
          <pubDate>{published.strftime('%a, %d %b %Y %H:%M:%S GMT')}</pubDate>
          <source url="https://publisher.example.com">Publisher</source>
          <description>Summary {index}</description>
        </item>
        """
        for index, (title, published) in enumerate(items)
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{body}</channel></rss>'


class _Response:
    def __init__(self, content, status=200):
        self.content = content
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def text(self):
        return self.content


class _Session:
    def __init__(self, response, **_kwargs):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def get(self, _url, **_kwargs):
        return self.response


async def test_only_a_successful_feed_advances_its_cursor(db, monkeypatch):
    source = make_source(
        kind="google_news",
        title="Google News",
        params={"regions": ["cn"], "topics": ["business"], "max_items_per_feed": 20},
    )
    now = datetime(2026, 8, 7, 12, tzinfo=timezone.utc)
    content = _rss(
        [
            ("After cursor", datetime(2026, 8, 7, 11, tzinfo=timezone.utc)),
            ("Before cursor", datetime(2026, 8, 7, 9, tzinfo=timezone.utc)),
        ]
    )

    with db() as session:
        gn.save_cursors(session, {"google-news-cn-business": "2026-08-07T10:00:00+00:00"})

    monkeypatch.setattr(
        "src.adapters.google_news.aiohttp.ClientSession",
        lambda **kwargs: _Session(_Response(content), **kwargs),
    )
    with db() as session:
        entries = await gn.fetch_google_news_entries(source, now - timedelta(hours=24), session, now=now)
        assert [e["title"] for e in entries] == ["After cursor"]
        assert gn.load_cursors(session)["google-news-cn-business"] == now.isoformat()

    monkeypatch.setattr(
        "src.adapters.google_news.aiohttp.ClientSession",
        lambda **kwargs: _Session(_Response("", status=503), **kwargs),
    )
    with db() as session:
        assert await gn.fetch_google_news_entries(
            source, now - timedelta(hours=24), session, now=now + timedelta(hours=1)
        ) == []
        # A failed feed keeps its cursor so the next cycle retries the same window.
        assert gn.load_cursors(session)["google-news-cn-business"] == now.isoformat()


async def test_google_news_lookback_never_exceeds_24_hours(db, monkeypatch):
    """A configured lookback wider than 24h must still be clamped (§5.6)."""
    source = make_source(
        kind="google_news",
        params={
            "regions": ["cn"],
            "topics": ["business"],
            "max_lookback_hours": 999,
            "max_items_per_feed": 20,
        },
    )
    now = datetime(2026, 8, 7, 12, tzinfo=timezone.utc)
    body = _rss(
        [
            ("Too old", now - timedelta(hours=48)),
            ("Recent", now - timedelta(hours=2)),
        ]
    )

    class _FeedSession:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def get(self, _url, **_kwargs):
            return _Response(body)

    monkeypatch.setattr(
        "src.adapters.google_news.aiohttp.ClientSession",
        lambda **kwargs: _FeedSession(**kwargs),
    )
    with db() as session:
        entries = await gn.fetch_google_news_entries(
            source, now - timedelta(hours=24), session, now=now
        )
    assert [e["title"] for e in entries] == ["Recent"]


# --------------------------------------------------------------------------- signals


async def test_only_enabled_subsources_are_requested(cutoff, monkeypatch):
    requested = []

    class _Session:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def get(self, url, **_kwargs):
            requested.append(url)
            raise RuntimeError("stop here")

        def post(self, url, **_kwargs):
            requested.append(url)
            raise RuntimeError("stop here")

    monkeypatch.setattr("src.adapters.signals.aiohttp.ClientSession", lambda **kwargs: _Session(**kwargs))
    source = make_source(params={"sources": {"v2ex": True}, "max_items_per_source": 2})

    assert await sig.fetch_signal_entries(source, cutoff) == []
    # v2ex walks its three nodes; nothing else in the catalog is touched.
    assert requested == ["https://www.v2ex.com/api/topics/show.json"] * len(sig.V2EX_NODES)


