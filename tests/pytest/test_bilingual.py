"""中英双语闭环回归测试。

覆盖三条链路：

* master 上报：slaver 用非主语言上报时，主表存主语言、源语言原文进
  ``signal_translation`` 且状态为 ``completed``；
* 读取与搜索：按请求 locale 序列化，主语言以外的 locale 不回退主表标题召回；
* 增量同步：新信号游标与迟到翻译游标各自推进，slaver 原地覆盖本地化文本
  而不动源语言正文。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.api import dependencies
from src.api.app import create_app
from src.auth import Identity
from src.db.bootstrap import run_migrations
from src.db.models import Sector, Signal, SignalTranslation
from src.db.repositories import (
    TRANSLATION_COMPLETED,
    TRANSLATION_PENDING,
    advance_sync_state,
    get_sync_state,
    queue_translation,
)
from src.db.session import (
    create_backend_indexes,
    dispose_engine,
    get_engine,
    session_scope,
)

MASTER_CONFIG = {
    "mode": "master",
    "output_language": "en",
    "content_languages": ["en", "zh"],
    "auth": {"main_url": "http://127.0.0.1:5678"},
    "sync": {"max_lookback_hours": 24, "page_size": 200},
}


@pytest.fixture
def db(tmp_path):
    dispose_engine()
    database_url = f"sqlite:///{tmp_path / 'bilingual.db'}"
    run_migrations(database_url)
    engine = get_engine(database_url, "news_agent")
    create_backend_indexes(engine)
    # The scoring chain refuses to run without an enabled sector, the same way a
    # fresh instance fails fast before the seed has run.
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
def master_app(db):
    return create_app(MASTER_CONFIG)


async def request(app, method: str, path: str, **kwargs):
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
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


# --------------------------------------------------------------- MCP 上报分流


class TestReportLanguageRouting:
    """master 收到非主语言上报时，主表与翻译表各写各的语言。"""

    @pytest.fixture
    def reporter(self, db, monkeypatch):
        async def fake_score_batch(entries, llm_config, sectors=None, duplicate_titles=None):
            from src.llm import SCORED_FLAG

            return (
                [
                    {
                        **entry,
                        "score": 80,
                        "quality_score": 80,
                        "interest_score": 80,
                        # 评分链重新生成主语言标题/摘要/关键词。
                        "title": "Nvidia raises guidance",
                        "summary": "English summary from scoring",
                        "keywords": ["nvidia"],
                        "assets": [],
                        "direction": "neutral",
                        "sectors": [],
                        SCORED_FLAG: True,
                    }
                    for entry in entries
                ],
                [],
            )

        async def fake_translate(items, locale, config):
            return {
                item["link"]: {
                    "title": "Nvidia raises guidance",
                    "summary": "Translated into the main locale",
                    "keywords": [],
                }
                for item in items
            }

        monkeypatch.setattr("src.llm.score_batch", fake_score_batch)
        monkeypatch.setattr(
            "src.master_mcp.translate_items", fake_translate, raising=False
        )
        import src.master_mcp as master_mcp

        monkeypatch.setattr(master_mcp, "translate_items", fake_translate)

        async def run(payload):
            scope = {"news_agent.identity": Identity(user_id="7")}
            request_obj = type("Request", (), {"scope": scope})()
            ctx = type(
                "Ctx",
                (),
                {
                    "request_context": type(
                        "RC", (), {"request": request_obj}
                    )()
                },
            )()
            return await master_mcp.report_signals([payload], ctx, MASTER_CONFIG)

        return run

    async def test_source_locale_text_is_stored_as_completed_translation(
        self, db, reporter
    ):
        result = await reporter(
            {
                "url": "https://reuters.com/zh-1",
                "title": "英伟达上调指引",
                "content": "公司上调全年展望。",
                "summary": "中文摘要",
                "source_name": "Reuters",
                "batch_id": "20261002T120000.000000Z-cycle-test",
                "source_language": "zh",
                "source_keywords": ["英伟达", "指引"],
            }
        )

        assert result["inserted"] == 1
        with session_scope() as session:
            row = session.query(Signal).one()
            # 主表是 master 的主语言。
            assert row.title == "Nvidia raises guidance"
            translation = session.query(SignalTranslation).one()
            # slaver 已经付过钱的原文直接落翻译表，不再翻一次。
            assert translation.locale == "zh"
            assert translation.title == "英伟达上调指引"
            assert translation.summary == "中文摘要"
            assert translation.keywords == ["英伟达", "指引"]
            assert translation.status == TRANSLATION_COMPLETED

    async def test_same_language_report_queues_a_translation(self, db, reporter):
        result = await reporter(
            {
                "url": "https://reuters.com/en-1",
                "title": "Nvidia raises guidance",
                "content": "The company lifted its outlook.",
                "source_name": "Reuters",
                "batch_id": "20261002T120000.000000Z-cycle-test",
                "source_language": "en",
            }
        )

        assert result["inserted"] == 1
        with session_scope() as session:
            translation = session.query(SignalTranslation).one()
            assert translation.locale == "zh"
            assert translation.status == TRANSLATION_PENDING
            assert translation.title == ""

    async def test_untranslatable_report_is_rejected_not_half_written(
        self, db, reporter, monkeypatch
    ):
        import src.master_mcp as master_mcp

        async def failing_translate(items, locale, config):
            raise RuntimeError("llm down")

        monkeypatch.setattr(master_mcp, "translate_items", failing_translate)

        result = await reporter(
            {
                "url": "https://reuters.com/zh-2",
                "title": "英伟达上调指引",
                "content": "公司上调全年展望。",
                "source_name": "Reuters",
                "batch_id": "20261002T120000.000000Z-cycle-test",
                "source_language": "zh",
            }
        )

        assert result["inserted"] == 0
        assert result["rejected"][0]["reason"] == "untranslated: item"
        assert result["received"] == (
            result["inserted"] + result["duplicates"] + len(result["rejected"])
        )
        with session_scope() as session:
            # 半条都不写：主表和翻译表都不能留下混语言残留。
            assert session.query(Signal).count() == 0
            assert session.query(SignalTranslation).count() == 0


# --------------------------------------------------------------- 读取与搜索


class TestLocaleReadAndSearch:
    async def test_target_locale_uses_the_completed_translation(self, db, master_app):
        now = datetime.now(timezone.utc) - timedelta(hours=1)
        add_signal("zh-1", now, title="English title", title_search="English title")
        with session_scope() as session:
            queue_translation(
                session,
                "zh-1",
                "zh",
                title="中文标题",
                summary="中文摘要",
                keywords=["英伟达"],
                status=TRANSLATION_COMPLETED,
            )

        response = await request(master_app, "GET", "/api/server/signals?locale=zh")
        item = response.json()["items"][0]
        assert item["title"] == "中文标题"
        assert item["summary"] == "中文摘要"
        assert item["keywords"] == ["英伟达"]
        assert item["fallback"] is False

    async def test_pending_translation_falls_back_to_the_main_table(self, db, master_app):
        now = datetime.now(timezone.utc) - timedelta(hours=1)
        add_signal("zh-2", now, title="English title", title_search="English title")
        with session_scope() as session:
            queue_translation(session, "zh-2", "zh", status=TRANSLATION_PENDING)

        response = await request(master_app, "GET", "/api/server/signals?locale=zh")
        item = response.json()["items"][0]
        assert item["title"] == "English title"
        assert item["fallback"] is True

    async def test_target_locale_search_ignores_main_table_text(self, db, master_app):
        """主 locale 不为目标 locale 召回，否则会返回用户读不懂的标题。"""
        now = datetime.now(timezone.utc) - timedelta(hours=1)
        add_signal(
            "zh-4",
            now,
            title="English title",
            title_search="English title",
        )
        with session_scope() as session:
            queue_translation(
                session,
                "zh-4",
                "zh",
                title="中文标题",
                summary="中文摘要",
                status=TRANSLATION_COMPLETED,
            )

        # 查询与入库用同一套分词：多词短语会被拆成 token 后 AND 匹配。
        hit = await request(
            master_app, "GET", "/api/server/signals?locale=zh&q=中文标题"
        )
        assert [item["id"] for item in hit.json()["items"]] == ["zh-4"]

        partial = await request(
            master_app, "GET", "/api/server/signals?locale=zh&q=中文摘要"
        )
        assert [item["id"] for item in partial.json()["items"]] == ["zh-4"]

        miss = await request(master_app, "GET", "/api/server/signals?locale=zh&q=English")
        assert miss.json()["items"] == []

# --------------------------------------------------------------- 增量同步


class TestSyncCursors:
    async def test_sync_returns_items_in_the_requested_locale(self, db, master_app):
        identity = Identity(user_id="7")
        master_app.dependency_overrides[dependencies.current_identity] = lambda: identity
        now = datetime.now(timezone.utc) - timedelta(hours=1)
        add_signal("zh-7", now, title="English title", title_search="English title")
        with session_scope() as session:
            queue_translation(
                session,
                "zh-7",
                "zh",
                title="中文标题",
                summary="中文摘要",
                status=TRANSLATION_COMPLETED,
            )
        try:
            response = await request(
                master_app, "GET", "/api/sync/signals?locale=zh"
            )
        finally:
            master_app.dependency_overrides.clear()

        payload = response.json()
        assert payload["locale"] == "zh"
        assert payload["items"][0]["title"] == "中文标题"
        # 同一窗口内既作为新信号返回，也作为迟到翻译返回：两者用不同游标，
        # slaver 侧按 URL 幂等处理。
        assert [row["url"] for row in payload["translations"]] == [
            "https://example.com/zh-7"
        ]

    async def test_pending_translation_is_not_published_to_slaver(self, db, master_app):
        identity = Identity(user_id="7")
        master_app.dependency_overrides[dependencies.current_identity] = lambda: identity
        now = datetime.now(timezone.utc) - timedelta(hours=1)
        add_signal("zh-9", now, title="English title", title_search="English title")
        with session_scope() as session:
            queue_translation(session, "zh-9", "zh", status=TRANSLATION_PENDING)
        try:
            response = await request(
                master_app, "GET", "/api/sync/signals?locale=zh"
            )
        finally:
            master_app.dependency_overrides.clear()

        assert response.json()["translations"] == []

    def test_the_two_cursors_advance_independently(self, db):
        signal_cursor = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
        translation_cursor = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)

        with session_scope() as session:
            advance_sync_state(session, signal_cursor, translation_cursor)

        with session_scope() as session:
            state = get_sync_state(session)
            assert state.last_pulled_at == signal_cursor
            assert state.last_translation_pulled_at == translation_cursor

class TestSlaverApplyTranslation:
    """slaver 侧原地覆盖本地化字段，源语言正文不动。"""

    def test_only_display_fields_are_overwritten(self, db):
        from src.orchestrator import apply_translation

        original_body = "公司上调全年展望。"
        with session_scope() as session:
            session.add(
                Signal(
                    id="zh-10",
                    url="https://example.com/zh-10",
                    url_hash="zh-10",
                    title="Nvidia raises guidance",
                    summary="English summary",
                    content=original_body,
                    content_search=original_body,
                    keywords=["nvidia"],
                    batch_id="batch-test",
                    score=80,
                    first_seen_at=datetime.now(timezone.utc),
                    source_name="Example",
                )
            )

        with session_scope() as session:
            assert apply_translation(
                session,
                {
                    "url": "https://example.com/zh-10",
                    "locale": "zh",
                    "title": "英伟达上调指引",
                    "summary": "中文摘要",
                    "keywords": ["英伟达"],
                },
            )

        with session_scope() as session:
            row = session.query(Signal).one()
            assert row.title == "英伟达上调指引"
            assert row.summary == "中文摘要"
            assert row.keywords == ["英伟达"]
            # 正文是源语言的，任何 locale 切换都不能改它。
            assert row.content == original_body
            assert row.content_search == original_body

# --------------------------------------------------------------- digest


class TestDigestLocale:
    async def test_digest_is_persisted_per_locale(self, db):
        from src.db.repositories import get_setting
        from src.push.digest import digest_key, push_digest

        now = datetime.now(timezone.utc) - timedelta(hours=1)
        add_signal(
            "zh-12",
            now,
            title="English title",
            title_search="English title",
            final_score=85.0,
        )
        with session_scope() as session:
            queue_translation(
                session,
                "zh-12",
                "zh",
                title="中文标题",
                summary="中文摘要",
                status=TRANSLATION_COMPLETED,
            )

        with session_scope() as session:
            await push_digest(session, MASTER_CONFIG, "zh")
        with session_scope() as session:
            digest = get_setting(session, digest_key("zh"), "")
        assert "中文标题" in digest
        # 主语言 digest 还没生成，legacy key 保持为空。
        with session_scope() as session:
            assert get_setting(session, "latest_digest", "") == ""

    async def test_latest_digest_route_serves_the_requested_locale(self, db, master_app):
        from src.db.repositories import set_setting
        from src.push.digest import digest_key

        with session_scope() as session:
            set_setting(
                session,
                digest_key("zh"),
                '{"locale": "zh", "hot": [{"title": "中文标题"}]}',
            )
            set_setting(
                session,
                "latest_digest",
                '{"locale": "en", "hot": [{"title": "English title"}]}',
            )

        zh = await request(master_app, "GET", "/api/server/latest-digest?locale=zh")
        assert zh.json()["hot"][0]["title"] == "中文标题"

        en = await request(master_app, "GET", "/api/server/latest-digest?locale=en")
        assert en.json()["hot"][0]["title"] == "English title"
