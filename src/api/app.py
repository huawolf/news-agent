from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.api.console import build_console_router, local_console_guard
from src.api.dependencies import admin_identity
from src.api.master import router as master_router
from src.api.reporters import router as reporters_router
from src.api.slaver import router as slaver_router
from src.api.ui import backoffice, portal, slaver_console
from src.app_logging import configure_logging
from src.config import get_mode, load_config
from src.config import get_translation_config
from src.db.bootstrap import bootstrap, resolve_owner
from src.db.session import dispose_engine
from src.db.session import session_scope
from src.db.repositories import get_setting
from src.config import get_server_url
from src.config import get_tokenizer_config
from src.dedup.tokenizer import configure as configure_tokenizer
from src.runtime import ensure_runtime_dirs

logger = logging.getLogger(__name__)


def _slaver_backoffice(request: Request):
    return slaver_console(request)


def create_app(config: dict | str | Path | None = None) -> FastAPI:
    if isinstance(config, dict):
        runtime_config = config
    else:
        runtime_config = load_config(str(config) if config else None)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        paths = ensure_runtime_dirs()
        configure_logging(
            paths["logs"],
            int((app.state.config.get("log") or {}).get("retention_days", 30)),
        )
        bootstrap(app.state.config)
        configure_logging(
            paths["logs"],
            int((app.state.config.get("log") or {}).get("retention_days", 30)),
        )
        tokenizer = get_tokenizer_config(app.state.config)
        configure_tokenizer(tokenizer["user_dict"], tokenizer["extra_stopwords"])
        scheduler = AsyncIOScheduler()
        mcp_client = None

        async def execute_fetch_cycle():
            from src.orchestrator import run_slaver_fetch_cycle
            from src.pipeline import run_fetch_cycle

            nonlocal mcp_client
            mode = get_mode(app.state.config)
            with session_scope() as session:
                owner = resolve_owner(session, app.state.config)
                if mode == "slaver" and get_server_url(app.state.config) and not owner.startswith("user:"):
                    return None
                user_id = owner.removeprefix("user:")
                if mode == "master":
                    return await run_fetch_cycle(session, app.state.config, owner=owner, is_master=True)
                token = get_setting(session, "manai_pic_jwt", "")
                from src.master_client import MasterMCPClient

                if mcp_client is None:
                    mcp_client = MasterMCPClient(app.state.config, token)
                else:
                    await mcp_client.update_token(token)
                return await run_slaver_fetch_cycle(
                    session, app.state.config, owner=owner, user_id=user_id,
                    token=token, mcp_client=mcp_client,
                )

        async def fetch_job() -> None:
            lock = app.state.fetch_lock
            if lock.locked():
                return
            await lock.acquire()
            app.state.fetch_state = {
                "status": "running",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "report": None,
            }
            try:
                report = await execute_fetch_cycle()
                app.state.fetch_state = {
                    "status": "completed",
                    "started_at": app.state.fetch_state["started_at"],
                    "report": report.__dict__ if report is not None else None,
                }
            except Exception:
                logger.exception("scheduled fetch cycle failed")
                app.state.fetch_state = {
                    "status": "failed",
                    "started_at": app.state.fetch_state["started_at"],
                    "report": None,
                    "message": "抓取失败，请检查本地日志。",
                }
            finally:
                lock.release()

        app.state.fetch_runner = execute_fetch_cycle

        async def push_job(locale: str = "") -> None:
            from src.push.digest import push_digest

            with session_scope() as session:
                await push_digest(session, app.state.config, locale)

        async def translation_job() -> None:
            # Own session and own lock: the queue is shared with the fetch
            # cycle that fills it, and a failed translation must never roll
            # back a fetch transaction.
            from src.translation import run_translation_cycle

            lock = app.state.translation_lock
            if lock.locked():
                return
            await lock.acquire()
            try:
                with session_scope() as session:
                    await run_translation_cycle(session, app.state.config)
            except Exception:
                logger.exception("scheduled translation cycle failed")
            finally:
                lock.release()

        def apply_schedule() -> None:
            """(Re)build the fetch and push jobs from the current config.

            Called once at startup and again whenever the console saves new
            delivery settings, so an interval or push-time change takes effect
            without a restart. The scheduler owns the jobs; the fetch interval
            lives in ``schedule.fetch_interval_minutes`` and the IM pushes in
            ``delivery.schedules`` (interpreted in
            ``delivery.timezone``).
            """
            if not scheduler.running:
                return
            schedule_config = app.state.config.get("schedule") or {}
            interval = int(schedule_config.get("fetch_interval_minutes", 60))
            scheduler.add_job(
                fetch_job,
                "interval",
                minutes=max(1, interval),
                id="fetch",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=300,
            )
            for job in scheduler.get_jobs():
                if job.id.startswith("push:"):
                    job.remove()
            delivery_config = app.state.config.get("delivery") or {}
            timezone_name = str(delivery_config.get("timezone") or "UTC")
            try:
                scheduler_timezone = ZoneInfo(timezone_name)
            except Exception:
                scheduler_timezone = ZoneInfo("UTC")
            for item in delivery_config.get("schedules", []):
                cron = item.get("cron") if isinstance(item, dict) else None
                if cron:
                    # A schedule may pin a locale; without one the digest is
                    # built in the main language.
                    schedule_locale = str((item or {}).get("locale") or "")
                    scheduler.add_job(
                        push_job,
                        CronTrigger.from_crontab(cron, timezone=scheduler_timezone),
                        id=f"push:{item.get('id', cron)}",
                        args=[schedule_locale],
                        replace_existing=True,
                        max_instances=1,
                        coalesce=True,
                        misfire_grace_time=300,
                    )

        translation_settings = get_translation_config(app.state.config)
        if translation_settings["enabled"]:
            scheduler.add_job(
                translation_job,
                "interval",
                minutes=translation_settings["interval_minutes"],
                id="translation",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=300,
            )
        app.state.apply_schedule = apply_schedule
        scheduler.start()
        apply_schedule()
        app.state.scheduler = scheduler
        mcp = None
        try:
            if get_mode(app.state.config) == "master":
                from src.master_mcp import mount_master_mcp, run_session_manager

                mcp = mount_master_mcp(app, app.state.config)
                async with run_session_manager(mcp):
                    yield
            else:
                yield
        finally:
            if mcp_client is not None:
                await mcp_client.close()
            if scheduler.running:
                scheduler.shutdown(wait=False)
            dispose_engine()

    app = FastAPI(title="News Agent", lifespan=lifespan)
    app.state.config = runtime_config
    app.state.fetch_lock = asyncio.Lock()
    app.state.translation_lock = asyncio.Lock()
    app.state.fetch_state = {"status": "idle", "started_at": None, "report": None}
    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).parent / "static"),
        name="portal-static",
    )
    # The operations console (LLM / RSS sources / preferences / fetch-once) is
    # one shared implementation: master guards it with the admin JWT, slaver
    # with loopback-only access. See ``src/api/console.py``.
    console_guard = admin_identity if get_mode(runtime_config) == "master" else local_console_guard
    app.include_router(build_console_router(console_guard))
    app.include_router(reporters_router)
    if get_mode(runtime_config) == "master":
        app.include_router(master_router)
        def master_portal(request: Request):
            return portal("master", request)

        app.add_api_route("/", master_portal, methods=["GET"])
    else:
        app.include_router(slaver_router)
        def slaver_portal(request: Request):
            return portal("slaver", request)

        app.add_api_route("/", slaver_portal, methods=["GET"])
    if get_mode(runtime_config) == "master":
        app.add_api_route("/backoffice", backoffice, methods=["GET"])
    else:
        app.add_api_route("/backoffice", _slaver_backoffice, methods=["GET"])
        app.add_api_route("/console", _slaver_backoffice, methods=["GET"] )
    return app
