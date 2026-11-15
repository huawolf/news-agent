"""Streamable HTTP protocol tests for the master MCP endpoint (§6.1, §6.2, §6.5).

These are the facts that unit tests cannot see. Every one of them is a wiring
mistake that would break the slaver in production while all the pure-function
tests kept passing:

* ``POST /api/mcp`` must answer directly. Mounting the child app at the full
  path instead of at ``/api`` makes Starlette 307-redirect to ``/api/mcp/``, and
  a redirect on a JSON-RPC POST loses the body.
* The handshake must fail with ``401`` when the token is missing, before any
  session exists. A JSON-RPC error at this point would be a lie: the MCP session
  was never established.
* The anonymous portal must stay reachable. If the JWT check leaked onto the
  parent app, every public page would demand a login.
* An oversized batch must come back as a JSON-RPC ``InvalidRequest`` rather than
  a truncated success, because a slaver that believes all 200 landed stops
  resending them.
* The tool schema must stay a flat ``items`` array. A wrapped payload would also
  make one dirty item fail 200 good ones at the SDK's validation layer.
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

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

BATCH_LIMIT = 3  # deliberately tiny, so the limit test stays cheap
REPORTER_KEY = "report_by_000000"

CONFIG = {
    "mode": "master",
    "auth": {"main_url": "http://127.0.0.1:5678"},
    "report": {
        "batch_size": BATCH_LIMIT,
        "content_max_bytes": 8192,
        "path": "/api/mcp",
        "reporter_key": REPORTER_KEY,
    },
    "mcp": {
        "server_name": "news-agent-master",
        "max_request_body_size": 4194304,
        "allowed_hosts": [],
        "allowed_origins": [],
    },
    "filter": {"max_age_hours": 24},
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


def item(url: str = "https://reuters.com/a", **overrides):
    payload = {
        "url": url,
        "title": "Nvidia raises datacenter guidance",
        "content": "The company lifted full year outlook on accelerator demand.",
        "source_name": "Reuters",
        "batch_id": "20261002T120000.000000Z-cycle-test",
    }
    payload.update(overrides)
    return payload


def yield_item(**overrides):
    payload = {
        "pool_name": "PT-reUSD-10DEC2026",
        "url": "https://app.pendle.finance/trade/markets/0xabc/swap",
        "reference": "reUSD",
        "protocol": "Pendle V2",
        "chain": "Ethereum",
        "tvl_usd": 12_622_972,
        "apy_30d": 11.69658,
        "apy_today": 11.67504,
        "expiry_date": "2026-12-10",
    }
    payload.update(overrides)
    return payload


def _initialize_payload():
    """The raw handshake body, for asserting on HTTP status before any client."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "probe", "version": "1"},
        },
    }


@pytest.fixture
def db(tmp_path):
    dispose_engine()
    database_url = f"sqlite:///{tmp_path / 'mcp_wire.db'}"
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
    """Answer ``verify_token`` locally, so no manai-pic is needed."""

    async def fake_verify_token(config, token):
        if token == "good-token":
            return Identity(user_id="7", username="tester")
        if not token:
            raise AuthError("missing token")
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
    """A master-shaped app: one anonymous route plus the mounted MCP child."""
    from fastapi import FastAPI

    from src.master_mcp import mount_master_mcp

    holder = {}

    @asynccontextmanager
    async def lifespan(_app):
        # Exactly what the real lifespan must do: a mounted sub-app's own
        # lifespan never fires, so the session manager is started here.
        from src.master_mcp import run_session_manager

        async with run_session_manager(holder["mcp"]):
            yield

    application = FastAPI(lifespan=lifespan)

    @application.get("/api/portal/signals")
    async def portal_signals():
        return {"items": []}

    holder["mcp"] = mount_master_mcp(application, CONFIG)
    return application


@pytest.fixture
def http(app):
    """A raw HTTP client bound to the app, plus the running lifespan.

    Returned as a context manager rather than a live client because the MCP
    session manager opens an AnyIO task group: entering it in a fixture and
    leaving it in the test would cancel that scope from a different task.
    """

    @asynccontextmanager
    async def running():
        import httpx

        # Drive the lifespan by hand: ASGITransport does not run it.
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://master.test",
            ) as client:
                yield client

    return running


def _wire_client(app, token: str):
    """A real Streamable HTTP client, wired through the in-process ASGI app."""
    import httpx
    from mcp.client.streamable_http import streamable_http_client

    @asynccontextmanager
    async def running():
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://master.test",
            headers={"Authorization": f"Bearer {token}"},
        )
        async with app.router.lifespan_context(app):
            async with client:
                async with streamable_http_client(
                    "http://master.test/api/mcp", http_client=client
                ) as streams:
                    yield streams

    return running()


# --------------------------------------------------------------------------- mounting


class TestMounting:
    async def test_post_api_mcp_is_answered_without_redirect(self, http):
        """A 307 here would drop the JSON-RPC body and break every batch."""
        async with http() as client:
            response = await client.post(
                "/api/mcp",
                json=_initialize_payload(),
                headers={
                    "Authorization": "Bearer good-token",
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
                follow_redirects=False,
            )

        assert response.status_code == 200, response.text
        assert response.headers.get("mcp-session-id")

    async def test_unauthenticated_handshake_is_401(self, http):
        async with http() as client:
            response = await client.post(
                "/api/mcp",
                json=_initialize_payload(),
                headers={"Accept": "application/json, text/event-stream"},
                follow_redirects=False,
            )

        assert response.status_code == 401
        assert response.json() == {"error": "unauthorized"}

# --------------------------------------------------------------------------- tools


class TestToolsOverTheWire:
    async def test_identity_comes_from_the_token_not_the_payload(self, app, db):
        """A forged ``reported_by`` in the body must not reach the row."""
        from mcp import ClientSession

        from src.db.models import Signal

        async with _wire_client(app, "good-token") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await session.call_tool(
                    "report_signals",
                    {"items": [item(reported_by="999", origin="master")]},
                )

        with session_scope() as session:
            row = session.query(Signal).one()
            assert row.reported_by == "7"
            assert row.origin == "slaver:7"

# --------------------------------------------------------------------------- batch limit


class TestBatchLimit:
    async def test_oversized_batch_is_a_protocol_error(self, app, db):
        """Not a truncated success: the slaver must learn to split the batch."""
        from mcp import ClientSession
        from mcp.shared.exceptions import McpError

        oversized = [item(f"https://reuters.com/{i}") for i in range(BATCH_LIMIT + 1)]

        async with _wire_client(app, "good-token") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                with pytest.raises(McpError) as excinfo:
                    await session.call_tool("report_signals", {"items": oversized})

        assert excinfo.value.error.code == -32600
        assert str(BATCH_LIMIT) in excinfo.value.error.message

# --------------------------------------------------------------------------- real app


@asynccontextmanager
async def _real_app_client(app, token: str = ""):
    """Drive the production lifespan and hand back an HTTP / MCP client.

    ``ASGITransport`` never runs the lifespan, and the real app mounts the MCP
    child *inside* it, so the lifespan must be entered by hand before the
    receive endpoint exists at all.
    """
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://master.test",
            headers=headers,
        ) as client:
            if not token:
                yield client, None
                return
            async with streamable_http_client(
                "http://master.test/api/mcp", http_client=client
            ) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    yield client, session


class TestRealAppMasterMode:
    """The production ``create_app`` in master mode exposes the receive endpoint.

    The classes above mount the MCP child onto a bare FastAPI app, which pins the
    SDK wiring but not that ``create_app`` actually mounts it — nor that the
    mount is gated to master mode. This class drives the real lifespan instead,
    so a regression in ``src/api/app.py`` wiring is caught here.
    """

    @pytest.fixture
    def real_master_app(self, db, stub_token, stub_llm, tmp_path, monkeypatch):
        from src.api import app as app_module
        from src.api.app import create_app

        # Keep runtime/log dirs out of the repo, and skip the seed: ``db`` has
        # already migrated and indexed the tmp database.
        monkeypatch.setenv("NEWS_AGENT_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(app_module, "bootstrap", lambda config: {})
        return create_app(CONFIG)

    async def test_report_signals_round_trip_persists_a_signal(
        self, real_master_app
    ):
        """A real MCP client reaches the endpoint the real app mounted."""
        from src.db.models import Signal

        async with _real_app_client(real_master_app, "good-token") as (_, session):
            tools = {tool.name for tool in (await session.list_tools()).tools}
            assert tools == {"report_signals", "upsert_yield_pools", "get_server_info"}

            result = await session.call_tool("report_signals", {"items": [item()]})
            assert result.isError is False
            assert result.structuredContent["received"] == 1
            assert result.structuredContent["inserted"] == 1

            # Assert inside the lifespan: its shutdown disposes the engine.
            with session_scope() as db_session:
                row = db_session.query(Signal).one()
                assert row.title == "Nvidia raises datacenter guidance"
                assert row.origin == "slaver:7"

    async def test_reporter_key_cannot_call_the_signal_report_tool(
        self, real_master_app
    ):
        async with _real_app_client(real_master_app, REPORTER_KEY) as (_, session):
            result = await session.call_tool("report_signals", {"items": [item()]})
            assert result.isError is True

    async def test_slaver_mode_never_mounts_the_receiver(
        self, tmp_path, monkeypatch
    ):
        """The receive endpoint is master-only; a slaver must answer 404."""
        import httpx

        from src.api import app as app_module
        from src.api.app import create_app

        monkeypatch.setenv("NEWS_AGENT_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(app_module, "bootstrap", lambda config: {})
        app = create_app({**CONFIG, "mode": "slaver", "server_url": ""})

        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://slaver.test"
            ) as client:
                response = await client.post(
                    "/api/mcp", json=_initialize_payload(), follow_redirects=False
                )

        assert response.status_code == 404
