"""Slaver-side MCP client tests (§6.1, §6.3, §6.5).

Unit level covers the contract mapping and failure classification; the wire
level runs a real master app in-process and drives it through the actual
Streamable HTTP client, because session reuse, batching offsets and 401
classification are exactly the facts a fake session cannot prove.
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.auth import AuthError, Identity
from src.db.bootstrap import run_migrations
from src.db.models import Sector
from src.db.session import (
    create_backend_indexes,
    dispose_engine,
    get_engine,
    session_scope,
)
from src.llm import SCORED_FLAG
from src.master_client import (
    MasterAuthRequired,
    MasterMCPClient,
    MasterReportError,
    to_report_item,
)

BATCH_LIMIT = 3

CONFIG = {
    "mode": "slaver",
    "server_url": "http://master.test",
    "auth": {"main_url": "http://127.0.0.1:5678"},
    "report": {"batch_size": BATCH_LIMIT, "content_max_bytes": 8192, "path": "/api/mcp"},
    "mcp": {
        "server_name": "news-agent-master",
        "max_request_body_size": 4194304,
        "allowed_hosts": [],
        "allowed_origins": [],
    },
    "filter": {"max_age_hours": 24},
    "ranking": {"title_weight": 3.0, "k1": 1.5, "b": 0.75},
    "dedup": {
        # Raised well above any score these fixtures can reach: these wire
        # tests exercise batching/session behaviour, not dedup. URL dedup runs
        # first and below any threshold, so the idempotent-resend assertions
        # still exercise the real duplicate path.
        "bm25_low_threshold": 50.0,
        "bm25_high_threshold": 60.0,
        "top_k": 10,
        "window_hours": 72,
        "same_source_bonus": 2.0,
    },
    "llm": {"model": "test", "max_prompt_chars": 10000},
}

# A master whose main ``signal`` table speaks English while serving Chinese too,
# so a Chinese report from a slaver exercises the cross-locale path.
MASTER_CONFIG = {
    **CONFIG,
    "mode": "master",
    "output_language": "en",
    "content_languages": ["en", "zh"],
    "translation": {"enabled": True, "batch_size": 10},
}

# The slaver side of that pair: it reports the locale its own users read.
SLAVER_CONFIG = {
    **CONFIG,
    "mode": "slaver",
    "output_language": "zh",
}


def signal(url: str = "https://reuters.com/a", **overrides):
    """A local signal row as the sync step would hand it over.

    Title/content are built from the URL's unique path segment so BM25 scores
    distinct rows well below ``bm25_low_threshold`` instead of treating them
    as near-duplicates of each other.
    """
    tag = url.rstrip("/").rsplit("/", 1)[-1]
    payload = {
        "url": url,
        "title": f"note-{tag} ref-{tag} {url}",
        "content": f"body-{tag} meta-{tag} context-{tag} {url}.",
        "source_name": f"Wire-{tag}",
        "batch_id": "20261002T120000.000000Z-cycle-test",
        # Master-side values that must never leave the slaver (§6.3).
        "score": 91,
        "sectors": ["AI技术"],
        "keywords": ["nvidia"],
        "origin": "slaver:7",
        "reported_by": "7",
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- mapping


class TestContractMapping:
    def test_internal_names_are_renamed_to_contract_names(self):
        item = to_report_item(
            {
                "link": "https://reuters.com/a",
                "source": "Reuters",
                "title": "T",
                "content": "C",
                "published": datetime(2026, 10, 2, 8, 31, tzinfo=timezone.utc),
                "batch_id": "20261002T120000.000000Z-cycle-test",
            },
            content_max_bytes=8192,
        )
        assert item["url"] == "https://reuters.com/a"
        assert item["source_name"] == "Reuters"
        assert item["published_at"] == "2026-10-02T08:31:00+00:00"
        assert item["batch_id"] == "20261002T120000.000000Z-cycle-test"
        assert "link" not in item
        assert "source" not in item

    def test_master_side_fields_are_dropped(self):
        item = to_report_item(signal(), content_max_bytes=8192)
        assert set(item) <= {
            "url",
            "title",
            "content",
            "summary",
            "source_name",
            "published_at",
            "assets",
            "direction",
            "batch_id",
            "source_language",
            "source_keywords",
        }

    def test_invalid_direction_is_omitted(self):
        item = to_report_item(signal(direction="to-the-moon"), content_max_bytes=8192)
        assert "direction" not in item

    def test_content_is_truncated_to_the_byte_budget(self):
        # Chinese content: 8192 chars is ~24 KB of UTF-8, so the cut must be
        # byte-based to keep 200-item batches under the 4 MiB body limit.
        text = "投" * 10000
        item = to_report_item(signal(content=text), content_max_bytes=100)
        encoded = item["content"].encode("utf-8")
        assert len(encoded) <= 100
        # No half character survived the cut.
        assert item["content"].encode("utf-8").decode("utf-8") == item["content"]


# --------------------------------------------------------------------------- failures


class TestFailureClassification:
    def _client(self, token: str = "good-token") -> MasterMCPClient:
        return MasterMCPClient(CONFIG, token)

    def test_401_becomes_auth_required_not_retryable(self):
        import httpx as hx

        request = hx.Request("POST", "http://master.test/api/mcp")
        response = hx.Response(401, request=request)
        error = hx.HTTPStatusError("401", request=request, response=response)

        with pytest.raises(MasterAuthRequired):
            self._client()._raise_classified(ExceptionGroup("g", [error]))

    def test_transport_error_is_retryable_report_error(self):
        with pytest.raises(MasterReportError):
            self._client()._raise_classified(ConnectionError("boom"))

    def test_missing_token_fails_before_any_request(self):
        async def run():
            client = self._client(token="")
            with pytest.raises(MasterAuthRequired):
                await client.connect()
            await client.close()

        import asyncio

        asyncio.run(run())


# --------------------------------------------------------------------------- wire


@pytest.fixture
def db(tmp_path):
    dispose_engine()
    database_url = f"sqlite:///{tmp_path / 'client.db'}"
    run_migrations(database_url)
    engine = get_engine(database_url, "news_agent")
    create_backend_indexes(engine)
    with session_scope() as session:
        session.add(
            Sector(
                id="sec-ai",
                name="AI技术",
                keywords=["ai chip"],
                enabled=True,
                sort_order=1,
            )
        )
    yield engine
    dispose_engine()


@pytest.fixture
def stub_token(monkeypatch):
    async def fake_verify_token(config, token):
        if token == "good-token":
            return Identity(user_id="7", username="tester")
        raise AuthError("token rejected by manai-pic")

    monkeypatch.setattr("src.master_mcp.verify_token", fake_verify_token)


@pytest.fixture
def stub_llm(monkeypatch):
    async def fake_score_batch(entries, llm_config, sectors=None, duplicate_titles=None):
        return [
            {
                **entry,
                "score": 82,
                "summary": "master summary",
                "quality_score": 80,
                "interest_score": 84,
                "keywords": ["nvidia"],
                "assets": ["NVDA"],
                "direction": "bullish",
                "sectors": ["AI技术"],
                SCORED_FLAG: True,
            }
            for entry in entries
        ], []

    monkeypatch.setattr("src.llm.score_batch", fake_score_batch)


@pytest.fixture
def app(db, stub_token, stub_llm):
    from fastapi import FastAPI

    from src.master_mcp import mount_master_mcp, run_session_manager

    holder = {}

    @asynccontextmanager
    async def lifespan(_app):
        async with run_session_manager(holder["mcp"]):
            yield

    application = FastAPI(lifespan=lifespan)
    holder["mcp"] = mount_master_mcp(application, CONFIG)
    return application


@asynccontextmanager
async def master_running(app):
    """Run the master lifespan and yield an ASGI transport for the client."""
    async with app.router.lifespan_context(app):
        yield httpx.ASGITransport(app=app)


def _client(transport, token: str = "good-token") -> MasterMCPClient:
    return MasterMCPClient(CONFIG, token, transport=transport)


class TestWire:
    async def test_report_reuses_one_session_and_splits_batches(self, app):
        """4 items with a batch limit of 3 → two batches, one session."""
        from src.db.models import Signal

        signals = [signal(f"https://reuters.com/{i}") for i in range(4)]

        async with master_running(app) as transport:
            client = _client(transport)
            try:
                result = await client.report(signals)
                first_session = client._session
                assert client.connected

                # Report again through the same session (idempotent resend).
                second = await client.report(signals[:1])
                assert client._session is first_session
            finally:
                await client.close()

        assert result["received"] == 4
        assert result["inserted"] == 4
        assert result["duplicates"] == 0
        assert result["rejected"] == []
        assert result["energy_credited"] == 4
        # The second (duplicate) call inserts nothing and credits no energy.
        assert second["inserted"] == 0
        assert second["duplicates"] == 1
        assert second["energy_credited"] == 0

        with session_scope() as session:
            assert session.query(Signal).count() == 4

    async def test_rejected_index_is_remapped_to_the_original_position(self, app):
        """The bad item sits at index 3 (batch 2, position 0) locally."""
        signals = [
            signal(f"https://reuters.com/{i}") for i in range(3)
        ] + [signal("https://reuters.com/bad", content="")]

        async with master_running(app) as transport:
            client = _client(transport)
            try:
                result = await client.report(signals)
            finally:
                await client.close()

        assert result["inserted"] == 3
        assert len(result["rejected"]) == 1
        assert result["rejected"][0]["index"] == 3
        assert result["rejected"][0]["reason"] == "missing_required_field: content"

# --------------------------------------------------------------------------- cross-locale


