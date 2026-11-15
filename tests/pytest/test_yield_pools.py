"""Yield-pool snapshot replacement (separate from scored news signals)."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.db.bootstrap import run_migrations
from src.db.models import Signal, YieldPool
from src.db.session import (
    create_backend_indexes,
    dispose_engine,
    get_engine,
    session_scope,
)

REPORTER_KEY = "report_by_000000"
CONFIG = {"report": {"reporter_key": REPORTER_KEY}}


@pytest.fixture
def db(tmp_path):
    dispose_engine()
    database_url = f"sqlite:///{tmp_path / 'yield_pools.db'}"
    run_migrations(database_url)
    create_backend_indexes(get_engine(database_url, "news_agent"))
    yield
    dispose_engine()


class FakeContext:
    pass


def payload(**overrides):
    item = {
        "pool_name": "PT-reUSD-10DEC2026",
        "url": "https://app.pendle.finance/trade/markets/0xabc/swap",
        "reference": "reUSD",
        "protocol": "Pendle V2",
        "chain": "Ethereum",
        "tvl_usd": 12_622_972.0,
        "apy_30d": 11.69658,
        "apy_today": 11.67504,
        "expiry_date": "2026-12-10",
    }
    item.update(overrides)
    return item


def _validate(item, index=0):
    from src.master_mcp import validate_yield_item

    return validate_yield_item(index, item)


def _replace(items, key=REPORTER_KEY):
    from src.master_mcp import upsert_yield_pools

    return upsert_yield_pools(items, key, FakeContext(), CONFIG)


class TestValidation:
    def test_minimal_item_is_accepted_and_expiry_is_parsed(self):
        checked = _validate(payload())
        assert checked["ok"] is True
        assert checked["entry"]["expiry_date"] == date(2026, 12, 10)
        assert checked["entry"]["tvl_usd"] == 12_622_972.0

    def test_expiry_key_must_be_present(self):
        item = payload()
        item.pop("expiry_date")
        checked = _validate(item, index=4)
        assert checked["ok"] is False
        assert checked["reject"]["reason"] == "missing_required_field: expiry_date"

    @pytest.mark.parametrize(
        ("field", "value", "reason"),
        [
            ("pool_name", "", "missing_required_field: pool_name"),
            ("reference", " ", "missing_required_field: reference"),
            ("protocol", "", "missing_required_field: protocol"),
            ("chain", "", "missing_required_field: chain"),
            ("url", "not-a-url", "invalid_url"),
            ("tvl_usd", 0, "invalid_number: tvl_usd"),
            ("tvl_usd", "NaN", "invalid_number: tvl_usd"),
            ("apy_30d", None, "invalid_number: apy_30d"),
            ("apy_today", "inf", "invalid_number: apy_today"),
            ("expiry_date", "10DEC2026", "invalid_date: expiry_date"),
        ],
    )
    def test_bad_field_is_rejected(self, field, value, reason):
        checked = _validate(payload(**{field: value}))
        assert checked["ok"] is False
        assert checked["reject"]["reason"] == reason

class TestSnapshotReplacement:
    async def test_first_report_inserts_and_records_reporter_key(self, db):
        result = await _replace([payload()])

        assert result == {
            "received": 1,
            "deleted": 0,
            "inserted": 1,
            "replaced": True,
            "rejected": [],
        }
        with session_scope() as session:
            row = session.query(YieldPool).one()
            assert row.reporter == REPORTER_KEY

    async def test_second_report_deletes_the_previous_snapshot(self, db):
        await _replace([payload()])
        result = await _replace(
            [payload(pool_name="fcUSDT", protocol="Morpho Blue", chain="Ethereum")]
        )

        assert result["deleted"] == 1
        assert result["inserted"] == 1
        assert result["replaced"] is True
        with session_scope() as session:
            rows = session.query(YieldPool).all()
            assert [row.pool_name for row in rows] == ["fcUSDT"]

    async def test_full_replacement_can_grow_or_shrink(self, db):
        await _replace([payload()])
        await _replace(
            [
                payload(),
                payload(
                    pool_name="fpUSDC",
                    protocol="Morpho Blue",
                    chain="Ethereum",
                    url="https://example.com/morpho",
                ),
            ]
        )
        assert await _replace([payload()]) == {
            "received": 1,
            "deleted": 2,
            "inserted": 1,
            "replaced": True,
            "rejected": [],
        }

    async def test_empty_snapshot_clears_the_table(self, db):
        await _replace([payload()])
        result = await _replace([])

        assert result["deleted"] == 1
        assert result["inserted"] == 0
        assert result["replaced"] is True
        with session_scope() as session:
            assert session.query(YieldPool).count() == 0

    async def test_one_bad_item_rejects_the_whole_snapshot_and_keeps_rows(self, db):
        await _replace([payload()])
        result = await _replace([payload(), payload(pool_name="")])

        assert result["replaced"] is False
        assert result["deleted"] == 0
        assert result["inserted"] == 0
        assert result["rejected"][0]["index"] == 1
        with session_scope() as session:
            rows = session.query(YieldPool).all()
            assert len(rows) == 1
            assert rows[0].pool_name == "PT-reUSD-10DEC2026"

    async def test_bad_reporter_key_is_rejected_before_any_delete(self, db):
        await _replace([payload()])
        with pytest.raises(ValueError, match="invalid reporter_key"):
            await _replace([], key="wrong-key")

        with session_scope() as session:
            assert session.query(YieldPool).count() == 1

    async def test_missing_configured_key_fails_closed(self, db, monkeypatch):
        from src.master_mcp import upsert_yield_pools

        monkeypatch.delenv("REPORTER_KEY", raising=False)
        monkeypatch.delenv("reporter_key", raising=False)

        with pytest.raises(ValueError, match="REPORTER_KEY is not configured"):
            await upsert_yield_pools(
                [payload()], REPORTER_KEY, FakeContext(), {"report": {}}
            )

        with session_scope() as session:
            assert session.query(YieldPool).count() == 0

    async def test_report_never_touches_signals(self, db):
        await _replace([payload()])

        with session_scope() as session:
            assert session.query(Signal).count() == 0
            assert session.query(YieldPool).count() == 1
