from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from src.api import dependencies
from src.api.app import create_app
from src.auth import Identity
from src.db.bootstrap import run_migrations
from src.db.models import FetchSource, Sector, Signal, Watchlist, YieldPool
from src.db.session import create_backend_indexes, dispose_engine, get_engine, session_scope


MASTER_CONFIG = {
    "mode": "master",
    "auth": {"main_url": "http://127.0.0.1:5678"},
    "sync": {"max_lookback_hours": 24, "page_size": 200},
}


@pytest.fixture
def db(tmp_path):
    dispose_engine()
    database_url = f"sqlite:///{tmp_path / 'api_regressions.db'}"
    run_migrations(database_url)
    engine = get_engine(database_url, "news_agent")
    create_backend_indexes(engine)
    yield engine
    dispose_engine()


@pytest.fixture
def master_app(db):
    return create_app(MASTER_CONFIG)


@pytest.fixture
def slaver_app(db):
    return create_app({"mode": "slaver"})


async def request(app, method: str, path: str, *, client=("127.0.0.1", 12345), **kwargs):
    transport = httpx.ASGITransport(app=app, client=client)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, **kwargs)


def add_signal(signal_id: str, first_seen_at: datetime, **values) -> None:
    values.setdefault("batch_id", "batch-test")
    values.setdefault("title", signal_id)
    values.setdefault("score", 80)
    with session_scope() as session:
        session.add(
            Signal(
                id=signal_id,
                url=f"https://example.com/{signal_id}",
                url_hash=signal_id,
                first_seen_at=first_seen_at,
                source_name="Example",
                **values,
            )
        )


def add_sector(name: str, *, enabled: bool = True) -> None:
    with session_scope() as session:
        session.add(Sector(id=name, name=name, enabled=enabled))


async def test_portal_uses_relative_api_paths(master_app):
    response = await request(master_app, "GET", "/")

    assert response.status_code == 200
    assert "signalEndpoint='api/server/signals'" in response.text
    assert "watchlistAssetsEndpoint='api/server/watchlist-assets'" in response.text
    assert "fetch('/api/server/signals')" not in response.text
    assert "githubEndpoint='api/server/github-trending'" in response.text
    assert "yieldEndpoint='api/server/yields'" in response.text


async def test_portal_uses_the_shared_svg_favicon(master_app):
    response = await request(master_app, "GET", "/")

    assert response.status_code == 200
    assert '<link rel="icon" type="image/svg+xml" href="static/favicon.svg?v=' in response.text

    favicon = await request(master_app, "GET", "/static/favicon.svg")
    assert favicon.status_code == 200
    assert favicon.headers["content-type"].startswith("image/svg+xml")
    assert favicon.text.lstrip().startswith("<svg")


async def test_portal_replaces_the_energy_badge_with_the_telegram_join_link(master_app):
    response = await request(master_app, "GET", "/")

    assert response.status_code == 200
    assert "account-energy-badge" not in response.text
    assert "account-energy-compact" not in response.text
    assert "class=telegram-join" in response.text
    assert 'href="https://telegram.me/+g1dlzFRWKwwxNzY0"' in response.text
    assert 'target="_blank"' in response.text
    assert 'rel="noopener noreferrer"' in response.text
    assert "data-i18n=joinTelegram" in response.text

    zh = await request(master_app, "GET", "/static/locales/zh/translation.json")
    en = await request(master_app, "GET", "/static/locales/en/translation.json")
    assert zh.json()["joinTelegram"] == "加入 Telegram"
    assert en.json()["joinTelegram"] == "Join Telegram"


async def test_portal_adds_the_yield_channel(master_app):
    response = await request(master_app, "GET", "/")

    assert response.status_code == 200
    assert 'class=channel-tabs' in response.text
    assert 'data-channel=signals' in response.text
    assert 'data-channel=yields' in response.text
    assert 'onclick="showYields()"' in response.text
    assert "function showSignalChannel" in response.text
    assert "yieldDeleteEndpoint" not in response.text
    assert "yield-summary" not in response.text
    assert "t('yieldReporter')" not in response.text
    assert "t('yieldRecordedAt')" not in response.text
    assert "[data-group=yields]" not in response.text

    i18n = await request(master_app, "GET", "/static/i18n.js")
    assert i18n.status_code == 200
    assert "t: (key, vars) => portalI18n.t(key, vars)" in i18n.text


async def test_yield_channel_explains_stablecoin_risk_in_both_languages(master_app):
    portal_page = await request(master_app, "GET", "/")
    assert "t('yieldNotice')" in portal_page.text

    zh = await request(master_app, "GET", "/static/locales/zh/translation.json")
    en = await request(master_app, "GET", "/static/locales/en/translation.json")
    assert zh.status_code == 200
    assert en.status_code == 200
    zh_notice = json.loads(zh.text)["yieldNotice"]
    en_notice = json.loads(en.text)["yieldNotice"]
    assert "USDT/USDC" in zh_notice
    assert "Pendle V2" in zh_notice
    assert "中途退出可能损失本金" in zh_notice
    assert "USDT/USDC" in en_notice
    assert "Pendle V2" in en_notice
    assert "exiting early" in en_notice


def add_yield_pool(
    pool_id: str,
    *,
    protocol: str = "Morpho Blue",
    chain: str = "Ethereum",
    apy_today: float = 10.0,
    apy_30d: float = 9.0,
) -> None:
    with session_scope() as session:
        session.add(
            YieldPool(
                id=pool_id,
                pool_name=pool_id,
                url=f"https://example.com/yields/{pool_id}",
                reference="USDC",
                protocol=protocol,
                chain=chain,
                tvl_usd=1_000_000,
                apy_30d=apy_30d,
                apy_today=apy_today,
                expiry_date=None,
                reporter="report_by_000000",
            )
        )


async def test_yield_table_is_sorted_by_today_apy(db, master_app):
    add_yield_pool("low", apy_today=4.2, apy_30d=8.0)
    add_yield_pool(
        "high",
        protocol="Pendle V2",
        apy_today=26.4,
        apy_30d=25.1,
    )
    add_yield_pool(
        "middle",
        protocol="Kamino Lend",
        chain="Solana",
        apy_today=12.7,
        apy_30d=11.9,
    )

    response = await request(master_app, "GET", "/api/server/yields")
    data = response.json()

    assert response.status_code == 200
    assert [item["pool_name"] for item in data["items"]] == ["high", "middle", "low"]
    assert data["count"] == 3
    assert data["protocols"] == ["Kamino Lend", "Morpho Blue", "Pendle V2"]
    assert data["chains"] == ["Ethereum", "Solana"]
    assert data["items"][0]["apy_today"] == 26.4
    assert "reporter" not in data["items"][0]
    assert "recorded_at" not in data["items"][0]
    assert "recorded_at" not in data
    assert "report_by_000000" not in response.text


async def test_master_deletes_yield_rows_by_protocol_and_chain(db, master_app):
    add_yield_pool("morpho-a")
    add_yield_pool("morpho-b", apy_today=11.0)
    add_yield_pool("pendle", protocol="Pendle V2", apy_today=20.0)

    denied = await request(
        master_app,
        "DELETE",
        "/api/backoffice/yields?protocol=Morpho%20Blue&chain=Ethereum",
    )
    assert denied.status_code == 401

    master_app.dependency_overrides[dependencies.admin_identity] = lambda: Identity(
        user_id="admin-1", is_admin=True
    )
    try:
        response = await request(
            master_app,
            "DELETE",
            "/api/backoffice/yields?protocol=Morpho%20Blue&chain=Ethereum",
        )
    finally:
        master_app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["deleted"] == 2
    with session_scope() as session:
        remaining = session.query(YieldPool).all()
        assert [row.pool_name for row in remaining] == ["pendle"]


async def test_master_login_uses_same_origin_and_proxy_prefix(master_app):
    response = await request(
        master_app,
        "GET",
        "/",
        headers={"X-Forwarded-Prefix": "/news"},
    )

    assert response.status_code == 200
    assert 'href="/login?redirect=%2Fnews%2F"' in response.text
    assert "vaneworker.com" not in response.text


async def test_feed_source_list_is_read_only_and_seed_backed(db, slaver_app):
    from src.db.repositories import create_source

    with session_scope() as session:
        create_source(
            session,
            "pending",
            "feed",
            "sample-feed",
            title="Sample Feed",
            params={"url": "https://feed.example/news"},
        )
    response = await request(slaver_app, "GET", "/api/sources/feed")
    assert response.status_code == 200
    assert response.json()["items"] == [
        {
            "kind": "feed",
            "key": "sample-feed",
            "title": "Sample Feed",
            "url": "https://feed.example/news",
            "enabled": True,
        }
    ]
    protected = await request(
        slaver_app,
        "PUT",
        "/api/sources/feed/sample-feed",
        json={"enabled": False},
    )
    assert protected.status_code == 400
    assert "only RSS" in protected.json()["detail"]


async def test_backoffice_gate_requires_admin_and_offers_login(master_app):
    """无权限时后台只显示提示与登录按钮，不再让用户粘贴 JWT。"""
    response = await request(master_app, "GET", "/backoffice")

    assert response.status_code == 200
    assert "需要管理权限，请登录后使用。" in response.text
    assert 'id=auth-login' in response.text
    assert 'href="https://vaneworker.com/login?redirect=%2Fnews%2Fbackoffice"' in response.text
    assert "if(!me||!me.is_admin){showGate();return}" in response.text
    assert "browserToken()" in response.text
    assert "粘贴 JWT" not in response.text
    assert "JWT 鉴权 · 配置与运行状态" not in response.text
    assert "返回门户" not in response.text


async def test_master_console_api_requires_admin_and_owns_public_rows(db, master_app):
    """master 与 slaver 共用同一份控制台接口，区别只在守卫与行归属。"""
    anonymous = await request(master_app, "GET", "/api/console/fetch-status")
    assert anonymous.status_code == 401

    master_app.dependency_overrides[dependencies.admin_identity] = lambda: Identity(user_id="admin-1", is_admin=True)
    try:
        status = await request(master_app, "GET", "/api/console/fetch-status")
        sources = await request(master_app, "GET", "/api/sources")
        created = await request(
            master_app,
            "POST",
            "/api/sources",
            json={"title": "Example", "xml_url": "https://example.com/feed.xml"},
        )
    finally:
        master_app.dependency_overrides.clear()

    assert status.status_code == 200 and status.json()["status"] == "idle"
    assert sources.status_code == 200 and sources.json()["owner"] == "public"
    assert created.status_code == 200 and created.json()["title"] == "Example"
    with session_scope() as session:
        owners = {row.owner for row in session.query(FetchSource).all()}
    assert owners == {"public"}


async def test_anonymous_signals_cover_seven_days_and_ignore_hours(db, master_app):
    now = datetime.now(timezone.utc)
    add_signal("fresh", now - timedelta(hours=2))
    add_signal("day-seven", now - timedelta(hours=167))
    add_signal("stale", now - timedelta(hours=169))

    without_hours = await request(master_app, "GET", "/api/server/signals")
    with_hours = await request(master_app, "GET", "/api/server/signals?hours=1")

    assert without_hours.status_code == 200
    assert with_hours.status_code == 200
    assert {item["title"] for item in without_hours.json()["items"]} == {"fresh", "day-seven"}
    assert {item["title"] for item in with_hours.json()["items"]} == {"fresh", "day-seven"}


async def test_master_profile_returns_upstream_account_energy_and_invite_code(master_app):
    identity = Identity(
        user_id="user-1",
        username="Researcher",
        membership="pro",
        membership_plan="MEMBER",
        energy_cached=48,
        self_code="INVITE1",
    )
    master_app.dependency_overrides[dependencies.current_identity] = lambda: identity
    try:
        response = await request(master_app, "GET", "/api/me")
    finally:
        master_app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["energy_cached"] == 48
    assert response.json()["self_code"] == "INVITE1"
    assert response.json()["energy_balance"] == 0


async def test_master_watchlist_is_the_shared_public_list(db, master_app):
    """写入与读取必须是同一个 owner，否则门户的资产筛选恒为空。

    §4.1 把 master 的关注列表定义为 ``owner = public`` 的那一份共享列表，
    门户匿名展示它。之前 PUT 写到 ``user:<id>`` 而 GET 读 ``public``，
    ``_watchlist_overlap()`` 永远拿不到资产，"我的关注" 页签因此永远是空的。
    """
    identity = Identity(user_id="user-1")
    master_app.dependency_overrides[dependencies.current_identity] = lambda: identity
    try:
        saved = await request(
            master_app,
            "PUT",
            "/api/me/watchlist",
            json={"items": [{"asset": "nvda", "asset_class": "stock"}]},
        )
        read_back = await request(master_app, "GET", "/api/me/watchlist")
    finally:
        master_app.dependency_overrides.clear()

    assert saved.status_code == 200
    assert read_back.status_code == 200
    assert [item["asset"] for item in read_back.json()["items"]] == ["NVDA"]

    with session_scope() as session:
        owners = {row.owner for row in session.query(Watchlist).all()}
    assert owners == {"public"}

    # 门户的资产筛选必须命中这份列表。
    add_signal("watched", datetime.now(timezone.utc) - timedelta(hours=1))
    with session_scope() as session:
        row = session.query(Signal).filter_by(id="watched").one()
        row.assets = ["NVDA"]
    response = await request(master_app, "GET", "/api/server/signals?group=watchlist&q=NVDA")
    assert [item["title"] for item in response.json()["items"]] == ["watched"]


async def test_public_signal_group_validates_filters_and_paginates(db, master_app):
    now = datetime.now(timezone.utc)
    add_sector("前沿科技")
    for index in range(3):
        add_signal(f"page-{index}", now - timedelta(minutes=index + 1), sectors=["前沿科技"], final_score=3 - index)
    response = await request(master_app, "GET", "/api/server/signals?group=hot&page=2&page_size=1")
    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == "page-1"
    assert response.json()["has_more"] is True
    assert (await request(master_app, "GET", "/api/server/signals?group=invalid")).status_code == 400
    assert (await request(master_app, "GET", "/api/server/signals?asset_class=unknown")).status_code == 200


async def test_public_search_covers_title_summary_content_and_assets(db, master_app):
    """User search covers the locale's title / summary / keywords plus the
    language-independent body and assets — but not sectors."""
    now = datetime.now(timezone.utc) - timedelta(hours=1)
    add_signal(
        "title-hit",
        now,
        title="Needle title",
        title_search="Needle title",
    )
    add_signal(
        "summary-hit",
        now,
        summary="Needle summary",
        summary_search="Needle summary",
    )
    add_signal(
        "keyword-hit",
        now,
        keywords=["Needle keyword"],
        keywords_search="Needle keyword",
    )
    add_signal(
        "content-hit",
        now,
        content="Needle body",
        content_search="Needle body",
    )
    add_signal("asset-hit", now, assets=["NEEDLE"])
    add_signal("sector-hit", now, sectors=["Needle sector"])

    for signal_id in (
        "title-hit",
        "summary-hit",
        "keyword-hit",
        "content-hit",
        "asset-hit",
    ):
        response = await request(master_app, "GET", "/api/server/signals?q=needle")
        assert signal_id in {item["id"] for item in response.json()["items"]}

    # Sectors are a filter dimension, never a search dimension.
    response = await request(master_app, "GET", "/api/server/signals?q=needle")
    assert "sector-hit" not in {item["id"] for item in response.json()["items"]}


async def test_search_ranks_asset_then_title_then_other_text_matches(db, master_app):
    now = datetime.now(timezone.utc) - timedelta(hours=1)
    add_signal(
        "content-hit-high-score",
        now,
        title="Market update",
        content="NVDA demand outlook",
        content_search="nvda demand outlook",
        final_score=99,
    )
    add_signal(
        "title-hit-mid-score",
        now,
        title="NVDA raises guidance",
        title_search="nvda raises guidance",
        final_score=60,
    )
    add_signal(
        "keyword-hit-lower-score",
        now,
        title="Semiconductor watch",
        title_search="semiconductor watch",
        keywords=["NVDA"],
        keywords_search="nvda",
        final_score=50,
    )
    add_signal(
        "asset-hit-low-score",
        now,
        title="Semiconductor market update",
        assets=["NVDA"],
        final_score=1,
    )

    response = await request(master_app, "GET", "/api/server/signals?q=NVDA")

    assert [item["id"] for item in response.json()["items"]] == [
        "asset-hit-low-score",
        "title-hit-mid-score",
        "keyword-hit-lower-score",
        "content-hit-high-score",
    ]


async def test_public_signals_sort_batches_before_score(db, master_app):
    now = datetime.now(timezone.utc) - timedelta(hours=1)
    add_signal("old-high", now, batch_id="20261003T100000Z-cycle-a", final_score=99)
    add_signal("new-low", now, batch_id="20261003T110000Z-cycle-b", final_score=1)
    add_signal("new-high", now, batch_id="20261003T110000Z-cycle-b", final_score=90)

    response = await request(master_app, "GET", "/api/server/signals")
    items = response.json()["items"]
    assert [item["id"] for item in items] == ["new-high", "new-low", "old-high"]
    assert [item["batch_id"] for item in items] == [
        "20261003T110000Z-cycle-b",
        "20261003T110000Z-cycle-b",
        "20261003T100000Z-cycle-a",
    ]


async def test_slaver_watchlist_and_rss_source_crud(db, slaver_app):
    watch = await request(
        slaver_app,
        "PUT",
        "/api/me/watchlist",
        json={"items": [{"asset": "nvda", "asset_class": "stock"}]},
    )
    watch_read = await request(slaver_app, "GET", "/api/me/watchlist")
    created = await request(
        slaver_app,
        "POST",
        "/api/sources",
        json={"title": "Example", "xml_url": "https://example.com/feed.xml"},
    )

    assert watch.status_code == 200
    assert watch_read.json()["items"] == [
        {"asset": "NVDA", "asset_class": "stock", "aliases": []}
    ]
    with session_scope() as session:
        assert session.query(Watchlist).filter_by(asset="NVDA").one().owner == "user:0"
    assert created.status_code == 200
    source = created.json()
    with session_scope() as session:
        row = session.query(FetchSource).filter_by(key=source["key"]).one()
        row.etag = '"old"'
        row.last_modified = "yesterday"

    updated = await request(
        slaver_app,
        "PUT",
        f"/api/sources/rss/{source['key']}",
        json={"title": "Updated", "xml_url": "https://example.com/new.xml", "enabled": False},
    )
    assert updated.status_code == 200
    assert updated.json()["title"] == "Updated"
    assert updated.json()["xml_url"] == "https://example.com/new.xml"
    assert updated.json()["enabled"] is False
    assert updated.json()["key"] != source["key"]
    with session_scope() as session:
        row = session.query(FetchSource).filter_by(key=updated.json()["key"]).one()
        assert row.etag == ""
        assert row.last_modified == ""

    deleted = await request(slaver_app, "DELETE", f"/api/sources/rss/{updated.json()['key']}")
    watch_deleted = await request(slaver_app, "PUT", "/api/me/watchlist", json={"items": []})
    invalid_class = await request(
        slaver_app,
        "PUT",
        "/api/me/watchlist",
        json={"items": [{"asset": "SPX", "asset_class": "index"}]},
    )
    assert deleted.json() == {"deleted": True}
    assert watch_deleted.json()["items"] == []
    assert invalid_class.status_code == 400


async def test_slaver_llm_config_stores_api_key_in_env_and_console_reads_it(db, slaver_app, tmp_path, monkeypatch):
    from src.api import console

    config_path = tmp_path / "config.json"
    config_path.write_text('{"mode":"slaver"}\n', encoding="utf-8")
    (tmp_path / ".env").write_text("OTHER=keep\nexport TEST_LLM_SECRET=old-value\n", encoding="utf-8")
    monkeypatch.setattr(console, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(console, "default_config_path", lambda: config_path)
    monkeypatch.delenv("TEST_LLM_SECRET", raising=False)

    saved = await request(
        slaver_app,
        "PUT",
        "/api/console/llm",
        json={
            "model": "test-model",
            "baseUrl": "https://llm.example.com/v1",
            "protocol": "openai_chat",
            "apiKeyName": "TEST_LLM_SECRET",
            "apiKey": "secret-value",
        },
    )
    loaded = await request(slaver_app, "GET", "/api/console/llm")
    saved_config = json.loads(config_path.read_text(encoding="utf-8"))
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")

    assert saved.status_code == 200
    assert saved.json() == {"saved": True, "key_configured": True}
    assert loaded.json()["key_configured"] is True
    # The PUT response never echoes the key; the loopback-only console GET does,
    # so the page can render it masked with a reveal toggle.
    assert "secret-value" not in saved.text
    assert loaded.json()["apiKey"] == "secret-value"
    assert "secret-value" not in config_path.read_text(encoding="utf-8")
    assert 'TEST_LLM_SECRET="secret-value"' in env_text
    assert saved_config["llm"]["model"] == "test-model"

    resaved = await request(
        slaver_app,
        "PUT",
        "/api/console/llm",
        json={
            "model": "test-model-2",
            "baseUrl": "https://llm.example.com/v1",
            "protocol": "openai_chat",
            "apiKey": "second-secret",
        },
    )
    assert resaved.status_code == 200
    assert 'TEST_LLM_SECRET="second-secret"' in (tmp_path / ".env").read_text(encoding="utf-8")
    assert "second-secret" not in config_path.read_text(encoding="utf-8")


async def test_slaver_console_preferences_roundtrip_and_loopback_only(db, slaver_app, tmp_path, monkeypatch):
    """本地控制台偏好设置：loopback 读写、写回 config.json、拒绝远程访问。"""
    from src.api import console

    config_path = tmp_path / "config.json"
    config_path.write_text('{"mode":"slaver"}\n', encoding="utf-8")
    monkeypatch.setattr(console, "default_config_path", lambda: config_path)

    loaded = await request(slaver_app, "GET", "/api/console/preferences")
    saved = await request(
        slaver_app,
        "PUT",
        "/api/console/preferences",
        json={"preferences": "  关注 AI 基础设施与量化证据  "},
    )
    remote = await request(
        slaver_app, "GET", "/api/console/preferences", client=("198.51.100.7", 4321)
    )

    assert loaded.status_code == 200 and loaded.json()["preferences"] == ""
    assert saved.status_code == 200
    assert saved.json()["preferences"] == "关注 AI 基础设施与量化证据"
    assert json.loads(config_path.read_text(encoding="utf-8"))["personal_preferences"] == "关注 AI 基础设施与量化证据"
    assert slaver_app.state.config["personal_preferences"] == "关注 AI 基础设施与量化证据"
    assert slaver_app.state.config["llm"]["personal_preferences"] == "关注 AI 基础设施与量化证据"
    assert remote.status_code == 403


async def test_slaver_fetch_once_runs_full_runner_and_rejects_overlap(slaver_app):
    import asyncio
    from dataclasses import dataclass

    started = asyncio.Event()
    finish = asyncio.Event()

    @dataclass
    class Report:
        inserted: int
        duplicates: int

    async def runner():
        started.set()
        await finish.wait()
        return Report(inserted=2, duplicates=1)

    slaver_app.state.fetch_runner = runner
    started_response = await request(slaver_app, "POST", "/api/console/fetch-once")
    await started.wait()
    running_status = await request(slaver_app, "GET", "/api/console/fetch-status")
    overlapping = await request(slaver_app, "POST", "/api/console/fetch-once")
    finish.set()
    await slaver_app.state.fetch_task
    completed = await request(slaver_app, "GET", "/api/console/fetch-status")

    assert started_response.status_code == 202
    assert running_status.json()["status"] == "running"
    assert running_status.json()["started_at"]
    assert overlapping.status_code == 409
    assert completed.json()["status"] == "completed"
    assert completed.json()["report"] == {"inserted": 2, "duplicates": 1}


async def test_standalone_slaver_fetch_uses_the_seeded_pending_owner(
    db, slaver_app, monkeypatch, tmp_path
):
    from src.api import app as app_module

    captured = {}

    async def fake_run(session, config, *, owner, user_id, token, **kwargs):
        captured.update(owner=owner, user_id=user_id, token=token)

    monkeypatch.setenv("NEWS_AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(app_module, "bootstrap", lambda config: {})
    monkeypatch.setattr("src.orchestrator.run_slaver_fetch_cycle", fake_run)

    async with slaver_app.router.lifespan_context(slaver_app):
        await slaver_app.state.fetch_runner()

    assert captured == {"owner": "pending", "user_id": "pending", "token": ""}
