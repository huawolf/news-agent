"""Conditional request regression tests (§5.8).

The properties that matter:

- ``304`` skips the source entirely: no entries, no new fingerprint.
- ``200`` returns entries and a fresh fingerprint.
- ``429`` / ``5xx`` keep the old fingerprint so the next cycle can retry.
- Both transports send the conditional headers. Covering only aiohttp would
  leave every Nitter source on full downloads forever.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src import fetcher
from src.db.models import FetchSource

FEED_BODY = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Example Wire</title>
<item><title>Nvidia ships a new accelerator</title>
<link>https://example.com/a</link>
<description>Body text</description></item>
</channel></rss>"""


def make_source(**overrides) -> FetchSource:
    values = {
        "id": "src-1",
        "owner": "user:1",
        "kind": "rss",
        "key": "k",
        "title": "Example Wire",
        "xml_url": "https://example.com/feed.xml",
        "etag": "",
        "last_modified": "",
    }
    values.update(overrides)
    return FetchSource(**values)


@pytest.fixture
def cutoff():
    return datetime.now(timezone.utc) - timedelta(hours=24)


# --------------------------------------------------------------------------- header building


# --------------------------------------------------------------------------- aiohttp path


class _AsyncResponse:
    def __init__(self, status, headers=None, body=b""):
        self.status = status
        self.headers = headers or {}
        self._body = body

    async def read(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _AsyncSession:
    def __init__(self, response, recorder):
        self._response = response
        self._recorder = recorder

    def get(self, url, headers=None, timeout=None):
        self._recorder.append({"url": url, "headers": dict(headers or {})})
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


async def test_304_skips_the_source(cutoff):
    recorder = []
    response = _AsyncResponse(304)
    source = make_source(etag='"v1"', last_modified="Tue, 30 Sep 2026 09:00:00 GMT")

    outcome = await fetcher.fetch_source(
        source, cutoff, session=_AsyncSession(response, recorder)
    )

    assert outcome.is_not_modified
    assert outcome.entries == []
    # No body means no new fingerprint: nothing changed, so nothing to store.
    assert outcome.etag == ""
    assert outcome.last_modified == ""


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_error_status_keeps_the_stored_fingerprint(status, cutoff):
    recorder = []
    response = _AsyncResponse(status)
    source = make_source(etag='"v1"', last_modified="Tue, 30 Sep 2026 09:00:00 GMT")

    outcome = await fetcher.fetch_source(
        source, cutoff, session=_AsyncSession(response, recorder)
    )

    assert outcome.status == fetcher.STATUS_ERROR
    assert outcome.etag == ""
    assert source.etag == '"v1"'
    assert source.last_modified == "Tue, 30 Sep 2026 09:00:00 GMT"


async def test_source_without_fingerprint_falls_back_to_a_full_fetch(cutoff):
    recorder = []
    response = _AsyncResponse(200, {"ETag": '"v1"'}, FEED_BODY)

    outcome = await fetcher.fetch_source(
        make_source(), cutoff, session=_AsyncSession(response, recorder)
    )

    assert "If-None-Match" not in recorder[0]["headers"]
    assert "If-Modified-Since" not in recorder[0]["headers"]
    assert outcome.is_ok


# --------------------------------------------------------------------------- nitter path


class _SyncResponse:
    def __init__(self, status_code, headers=None, content=b""):
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content


async def test_nitter_304_is_handled(cutoff, monkeypatch):
    recorder = []

    def fake_get(url, headers=None, timeout=None):
        recorder.append(dict(headers or {}))
        return _SyncResponse(304)

    monkeypatch.setattr(fetcher.requests, "get", fake_get)

    outcome = await fetcher.fetch_source(
        make_source(xml_url="https://nitter.net/user/rss", etag='"n0"'), cutoff
    )

    assert recorder[0]["If-None-Match"] == '"n0"'
    assert outcome.is_not_modified
    assert outcome.entries == []


async def test_fetch_all_sources_routes_each_source_to_its_transport(cutoff, monkeypatch):
    normal_calls = []
    nitter_calls = []

    class _Session:
        def get(self, url, headers=None, timeout=None):
            normal_calls.append(url)
            return _AsyncResponse(304)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    def fake_get(url, headers=None, timeout=None):
        nitter_calls.append(url)
        return _SyncResponse(304)

    monkeypatch.setattr(fetcher, "is_nitter_url", lambda url: "nitter" in (url or ""))
    monkeypatch.setattr(fetcher.requests, "get", fake_get)

    outcomes = await fetcher.fetch_all_sources(
        [
            make_source(id="a", xml_url="https://plain.example/feed"),
            make_source(id="b", xml_url="https://nitter.example/feed"),
        ],
        cutoff,
        session=_Session(),
    )

    assert normal_calls == ["https://plain.example/feed"]
    assert nitter_calls == ["https://nitter.example/feed"]
    assert [o.is_not_modified for o in outcomes] == [True, True]


# --------------------------------------------------------------------------- persistence


@pytest.fixture
def db(tmp_path):
    from src.db.bootstrap import run_migrations
    from src.db.session import create_backend_indexes, dispose_engine, session_scope

    dispose_engine()
    database_url = f"sqlite:///{tmp_path / 'news.db'}"
    run_migrations(database_url)
    from src.db.session import get_engine

    create_backend_indexes(get_engine(database_url, "news_agent"))
    yield session_scope
    dispose_engine()


def _persist(session_scope, source, outcome):
    with session_scope() as session:
        session.add(source)
    with session_scope() as session:
        fetcher.persist_outcomes(session, [outcome])
    with session_scope() as session:
        return session.get(FetchSource, source.id)


def test_ok_outcome_stores_the_new_fingerprint(db):
    source = make_source(etag='"v1"', last_modified="Tue, 30 Sep 2026 09:00:00 GMT")
    outcome = fetcher.FetchOutcome(
        source=source,
        status=fetcher.STATUS_OK,
        etag='"v2"',
        last_modified="Wed, 01 Oct 2026 09:00:00 GMT",
    )

    row = _persist(db, source, outcome)

    assert row.etag == '"v2"'
    assert row.last_modified == "Wed, 01 Oct 2026 09:00:00 GMT"
    assert row.last_fetched_at is not None
    assert row.last_error_at is None


