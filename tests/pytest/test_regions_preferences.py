"""Region tagging, filtering and per-user preference contracts."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from src.api import dependencies
from src.api.app import create_app
from src.auth import Identity
from src.db.bootstrap import run_migrations
from src.db.models import Signal
from src.db.repositories import (
    get_user_preferences,
    get_user_regions,
    get_user_signal_settings,
    recent_signals,
    set_user_regions,
    set_user_signal_settings,
)
from src.db.session import dispose_engine, get_engine, session_scope
from src.preferences import normalize_signal_settings
from src.regions import (
    DEFAULT_USER_REGIONS,
    normalize_signal_region,
    normalize_user_regions,
)


PROJECT_ROOT = Path(__file__).parents[2]


@pytest.fixture
def db(tmp_path):
    dispose_engine()
    database_url = f"sqlite:///{tmp_path / 'regions.db'}"
    run_migrations(database_url)
    engine = get_engine(database_url, "news_agent")
    yield engine
    dispose_engine()


@pytest.fixture
def master_app(db):
    return create_app({"mode": "master", "auth": {"main_url": "http://127.0.0.1:5678"}})


async def request(app, method: str, path: str, **kwargs):
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, **kwargs)


def add_signal(signal_id: str, *, region: str | None, sectors: list[str] | None = None):
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        session.add(
            Signal(
                id=signal_id,
                url=f"https://example.com/{signal_id}",
                url_hash=signal_id,
                batch_id="batch-regions",
                title=signal_id,
                first_seen_at=now,
                score=80,
                final_score=90,
                region=region,
                sectors=sectors or [],
            )
        )


class TestRegionNormalization:
    def test_region_sensitive_sector_keeps_valid_region(self):
        assert normalize_signal_region("CN", ["股票"]) == "CN"
        assert normalize_signal_region("jp", ["宏观经济"]) == "JP"

    def test_region_agnostic_sectors_force_null(self):
        assert normalize_signal_region("CN", ["前沿科技"]) is None
        assert normalize_signal_region("CN", ["加密货币"]) is None
        assert normalize_signal_region("CN", ["大宗商品"]) is None

class TestPreferenceNormalization:
    def test_subset_is_deduplicated_and_canonical(self):
        assert normalize_user_regions(["JP", "CN", "JP"]) == ["CN", "JP"]

    def test_invalid_signal_settings_are_rejected(self):
        with pytest.raises(ValueError):
            normalize_signal_settings({"directions": [], "min_score": 80})
        with pytest.raises(ValueError):
            normalize_signal_settings({"directions": ["bullish"], "min_score": 101})
        with pytest.raises(ValueError):
            normalize_signal_settings({"directions": ["sideways"], "min_score": 80})


class TestRegionFiltering:
    def test_region_filter_keeps_regionless_signals(self, db):
        add_signal("cn-stock", region="CN", sectors=["股票"])
        add_signal("global-stock", region="GLOBAL", sectors=["股票"])
        add_signal("crypto", region=None, sectors=["加密货币"])
        with session_scope() as session:
            ids = {row.id for row in recent_signals(session, regions=["CN"])}
        assert ids == {"cn-stock", "crypto"}

class TestUserPreferenceStorage:
    def test_default_and_persisted_blocks(self, db):
        with session_scope() as session:
            initial = get_user_preferences(session, "user-1")
            assert initial == {
                "regions": DEFAULT_USER_REGIONS,
                "signal_settings": None,
                "initialized": False,
            }
            assert get_user_regions(session, "user-1") == DEFAULT_USER_REGIONS
            assert set_user_regions(session, "user-1", ["JP", "CN"]) == ["CN", "JP"]
            assert set_user_signal_settings(
                session,
                "user-1",
                {"directions": ["neutral", "bullish"], "min_score": 72},
            ) == {"directions": ["bullish", "neutral"], "min_score": 72}
        with session_scope() as session:
            assert get_user_regions(session, "user-1") == ["CN", "JP"]
            assert get_user_signal_settings(session, "user-1") == {
                "directions": ["bullish", "neutral"],
                "min_score": 72,
            }
            assert get_user_preferences(session, "user-1") == {
                "regions": ["CN", "JP"],
                "signal_settings": {"directions": ["bullish", "neutral"], "min_score": 72},
                "initialized": True,
            }


class TestPreferenceApi:
    async def test_get_default_and_put_subset(self, master_app):
        identity = Identity(user_id="user-1")
        master_app.dependency_overrides[dependencies.current_identity] = lambda: identity
        try:
            default = await request(master_app, "GET", "/api/me/preferences")
            saved = await request(
                master_app,
                "PUT",
                "/api/me/preferences",
                json={
                    "regions": ["JP", "CN"],
                    "signal_settings": {"directions": ["neutral", "bullish"], "min_score": 75},
                },
            )
            loaded = await request(master_app, "GET", "/api/me/preferences")
            invalid = await request(
                master_app,
                "PUT",
                "/api/me/preferences",
                json={"regions": ["US"]},
            )
            invalid_settings = await request(
                master_app,
                "PUT",
                "/api/me/preferences",
                json={"signal_settings": {"directions": [], "min_score": 80}},
            )
        finally:
            master_app.dependency_overrides.clear()

        assert default.json()["regions"] == DEFAULT_USER_REGIONS
        assert default.json()["signal_settings"] is None
        assert default.json()["initialized"] is False
        assert saved.json()["regions"] == ["CN", "JP"]
        assert saved.json()["signal_settings"] == {"directions": ["bullish", "neutral"], "min_score": 75}
        assert saved.json()["initialized"] is True
        assert loaded.json() == saved.json()
        assert invalid.status_code == 400
        assert invalid_settings.status_code == 400


