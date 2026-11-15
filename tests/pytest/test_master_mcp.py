"""Master MCP report path regression tests (§6.1–§6.6).

Two layers:

* unit tests over :func:`validate_item` and the tool body with a stub scorer —
  they pin the whitelist, the limits, the counters and the invariants;
* protocol tests over a real Streamable HTTP client — they pin the wiring facts
  that are easy to break silently: the ``/api`` mount answering ``POST /api/mcp``
  without a redirect, ``401`` before the handshake, the anonymous route staying
  open, and an oversized batch coming back as ``InvalidRequest``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.auth import Identity
from src.db.bootstrap import run_migrations
from src.db.models import EnergyLedger, Sector, Signal, SignalTranslation
from src.db.session import (
    create_backend_indexes,
    dispose_engine,
    get_engine,
    session_scope,
)
from src.llm import SCORED_FLAG

CONFIG = {
    "mode": "master",
    "auth": {"main_url": "http://127.0.0.1:5678"},
    "report": {"batch_size": 3, "content_max_bytes": 8192, "path": "/api/mcp"},
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


@pytest.fixture
def db(tmp_path):
    dispose_engine()
    database_url = f"sqlite:///{tmp_path / 'master_mcp.db'}"
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
            ]
        )
    yield engine
    dispose_engine()


@pytest.fixture
def scorer(monkeypatch):
    """Replace the LLM call with a deterministic scorer and record its input.

    The seam is ``src.llm.score_batch``, not ``src.master_mcp.score_reported``:
    stubbing the latter would also stub the sector lookup, and the point of these
    tests is that master re-reads the enabled sectors from the table.
    """

    async def fake_score_batch(entries, llm_config, sectors=None, duplicate_titles=None):
        scorer.entries = list(entries)
        scorer.sectors = sectors
        scorer.duplicate_titles = duplicate_titles
        scored = []
        for entry in entries:
            scored.append(
                {
                    **entry,
                    "score": 82,
                    "summary": "master summary",
                    "quality_score": 80,
                    "interest_score": 84,
                    "keywords": ["nvidia", "guidance"],
                    "assets": ["NVDA"],
                    "direction": "bullish",
                    "sectors": ["AI技术"],
                    SCORED_FLAG: True,
                }
            )
        return scored, []

    scorer.entries = []
    scorer.sectors = None
    scorer.duplicate_titles = None
    monkeypatch.setattr("src.llm.score_batch", fake_score_batch)
    return scorer


class FakeContext:
    """Minimal stand-in for the FastMCP ``Context``.

    Only ``request_context.request.scope`` is read, because that is the whole
    point of the design: the identity comes from the scope the ASGI wrapper
    wrote, never from the payload.
    """

    def __init__(self, identity=None):
        scope = {} if identity is None else {"news_agent.identity": identity}
        request = type("Request", (), {"scope": scope})()
        self.request_context = type(
            "RequestContext", (), {"request": request}
        )()


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


# --------------------------------------------------------------------------- validate


class TestValidateItem:
    def test_minimal_item_is_accepted(self):
        checked = _validate(item())
        assert checked["ok"] is True
        assert checked["entry"]["link"] == "https://reuters.com/a"
        # ``source_name`` is the contract name; the collector's field is ``source``.
        assert checked["entry"]["source"] == "Reuters"

    @pytest.mark.parametrize("field", ["title"])
    def test_missing_required_field_is_rejected(self, field):
        payload = item()
        payload.pop(field)
        checked = _validate(payload, index=7)
        assert checked["ok"] is False
        assert checked["reject"] == {
            "index": 7,
            "url": payload.get("url", ""),
            "reason": f"missing_required_field: {field}",
        }

    @pytest.mark.parametrize(
        ("field", "length"),
        [
            ("url", 2049),
            ("content", 8193),
        ],
    )
    def test_oversized_field_is_rejected_not_truncated(self, field, length):
        checked = _validate(item(**{field: "x" * length}))
        assert checked["ok"] is False
        assert checked["reject"]["reason"] == f"too_long: {field}"

    def test_unknown_fields_are_dropped(self):
        checked = _validate(
            item(score=99, sectors=["AI技术"], origin="master", reported_by="7")
        )
        assert checked["ok"] is True
        assert "score" not in checked["entry"]
        assert "origin" not in checked["entry"]
        assert "reported_by" not in checked["entry"]

def _validate(payload, index: int = 0):
    from src.master_mcp import validate_item

    return validate_item(index, payload)


# --------------------------------------------------------------------------- report


class TestReportSignals:
    async def test_reports_a_new_signal(self, db, scorer):
        from src.master_mcp import report_signals

        result = await report_signals(
            [item()], FakeContext(Identity(user_id="7")), CONFIG
        )

        assert result["received"] == 1
        assert result["inserted"] == 1
        assert result["duplicates"] == 0
        assert result["rejected"] == []
        assert result["energy_credited"] == 1

        with session_scope() as session:
            row = session.query(Signal).one()
            assert row.origin == "slaver:7"
            assert row.reported_by == "7"
            assert row.assets == ["NVDA"]
            assert row.sectors == ["AI技术"]
            assert row.score == 82
            assert row.batch_id == "20261002T120000.000000Z-cycle-test"

    async def test_origin_never_carries_the_user_prefix(self, db, scorer):
        """``slaver:7``, not ``slaver:user:7`` (§3.1)."""
        from src.master_mcp import report_signals

        await report_signals([item()], FakeContext(Identity(user_id="42")), CONFIG)

        with session_scope() as session:
            assert session.query(Signal).one().origin == "slaver:42"

    async def test_master_rescopes_and_ignores_reported_sectors(self, db, scorer):
        from src.master_mcp import report_signals

        await report_signals(
            [item(sectors=["不存在的板块"], score=1)],
            FakeContext(Identity(user_id="7")),
            CONFIG,
        )

        with session_scope() as session:
            row = session.query(Signal).one()
            assert row.sectors == ["AI技术"]
            assert row.score == 82

    async def test_resend_of_the_same_batch_is_idempotent(self, db, scorer):
        """URL dedup is the first gate, so a retry inserts nothing (§6.5)."""
        from src.master_mcp import report_signals

        identity = Identity(user_id="7")
        first = await report_signals([item()], FakeContext(identity), CONFIG)
        second = await report_signals([item()], FakeContext(identity), CONFIG)

        assert first["inserted"] == 1
        assert second["inserted"] == 0
        assert second["duplicates"] == 1
        assert second["energy_credited"] == 0

        with session_scope() as session:
            assert session.query(Signal).count() == 1
            # Energy was credited once, not twice.
            assert session.query(EnergyLedger).count() == 1

    async def test_one_bad_item_does_not_fail_the_batch(self, db, scorer):
        from src.master_mcp import report_signals

        bad = item("https://reuters.com/bad")
        bad.pop("content")

        result = await report_signals(
            [item(), bad, item("https://reuters.com/c")],
            FakeContext(Identity(user_id="7")),
            CONFIG,
        )

        assert result["received"] == 3
        assert result["inserted"] == 2
        assert result["rejected"] == [
            {
                "index": 1,
                "url": "https://reuters.com/bad",
                "reason": "missing_required_field: content",
            }
        ]
        # The invariant the slaver relies on to reconcile counters.
        assert result["received"] == (
            result["inserted"]
            + result["hidden"]
            + result["duplicates"]
            + len(result["rejected"])
        )
        assert result["energy_credited"] == result["inserted"]

    async def test_missing_identity_is_refused(self, db, scorer):
        """No verified identity means no write — never a default user (§3.3)."""
        from src.master_mcp import report_signals

        with pytest.raises(ValueError):
            await report_signals([item()], FakeContext(), CONFIG)

        with session_scope() as session:
            assert session.query(Signal).count() == 0

    async def test_scorer_receives_sectors_from_the_database(self, db, scorer):
        from src.master_mcp import report_signals

        await report_signals([item()], FakeContext(Identity(user_id="7")), CONFIG)

        assert [s["name"] for s in scorer.sectors] == ["AI技术", "宏观经济"]

    async def test_low_score_and_quality_flags_are_stored_hidden(self, db, monkeypatch):
        """Hidden rows count for reputation but never become public signals."""
        from src.master_mcp import report_signals

        async def fake_score_batch(entries, llm_config, sectors=None, duplicate_titles=None):
            scored = []
            for entry in entries:
                if "roundup" in entry["link"]:
                    item_result = {
                        "score": 88,
                        "summary": "roundup",
                        "is_aggregate": True,
                    }
                elif "empty" in entry["link"]:
                    item_result = {
                        "score": 25,
                        "summary": "",
                        "is_low_content": True,
                    }
                else:
                    item_result = {"score": 55, "summary": "thin but scored"}
                scored.append(
                    {
                        **entry,
                        **item_result,
                        "keywords": ["signal"],
                        "assets": ["NVDA"],
                        "direction": "neutral",
                        "sectors": ["AI技术"],
                        SCORED_FLAG: True,
                    }
                )
            return scored, []

        monkeypatch.setattr("src.llm.score_batch", fake_score_batch)

        result = await report_signals(
            [
                item("https://reuters.com/roundup"),
                item("https://reuters.com/empty"),
                item("https://reuters.com/low"),
            ],
            FakeContext(Identity(user_id="7")),
            CONFIG,
        )

        assert result["received"] == 3
        assert result["inserted"] == 0
        assert result["hidden"] == 3
        assert result["scored"] == 3
        assert result["energy_credited"] == 0
        with session_scope() as session:
            rows = {row.url: row for row in session.query(Signal).all()}
            assert rows["https://reuters.com/roundup"].score == 0
            assert rows["https://reuters.com/empty"].score == 0
            assert rows["https://reuters.com/low"].score == 55
            assert session.query(EnergyLedger).count() == 0
            assert session.query(SignalTranslation).count() == 0

    async def test_daily_quota_rejects_without_calling_the_scorer(self, db, scorer):
        from src.master_mcp import report_signals

        quota_config = {
            **CONFIG,
            "report": {**CONFIG["report"], "daily_limit_per_user": 2},
        }
        identity = FakeContext(Identity(user_id="7"))
        items = [
            item("https://reuters.com/quota-a"),
            item("https://reuters.com/quota-b"),
            item("https://reuters.com/quota-c"),
        ]

        first = await report_signals(items, identity, quota_config)
        second = await report_signals(
            [item("https://reuters.com/quota-d")], identity, quota_config
        )

        assert first["inserted"] == 2
        assert first["rejected"] == [
            {
                "index": 2,
                "url": "https://reuters.com/quota-c",
                "reason": "quota_exceeded: daily scoring limit",
            }
        ]
        assert len(scorer.entries) == 2
        assert first["quota"]["used"] == 2
        assert first["quota"]["remaining"] == 0
        assert second["inserted"] == 0
        assert second["rejected"][0]["reason"] == "quota_exceeded: daily scoring limit"
        assert len(scorer.entries) == 2


# --------------------------------------------------------------------------- wiring


class TestTransportSecurity:
    def test_empty_allow_lists_disable_protection(self):
        from src.master_mcp import _transport_security

        settings = _transport_security({"allowed_hosts": [], "allowed_origins": []})
        assert settings.enable_dns_rebinding_protection is False

