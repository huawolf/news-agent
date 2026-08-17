"""Single execution boundary for scheduled, Web, API and MCP jobs."""

import asyncio
import contextlib
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from src.app_logging import current_log_dir
from src.config_service import ConfigService


class JobExecutor:
    def __init__(self, config_service: ConfigService):
        self.config_service = config_service
        self._locks = {"fetch": asyncio.Lock(), "push": asyncio.Lock(), "preview": asyncio.Lock(), "run": asyncio.Lock()}
        self._running: dict[str, str] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self.log = logging.getLogger("news_agent.app")

    def status(self) -> dict:
        return {"running": dict(self._running), "task_count": len(self._tasks)}

    def submit(self, kind: str, source: str = "api") -> dict:
        if kind not in self._locks:
            raise ValueError(f"unsupported job type: {kind}")
        if kind in self._running:
            return {"id": self._running[kind], "status": "running", "deduplicated": True}
        job_id = uuid.uuid4().hex
        task = asyncio.create_task(self._run(job_id, kind, source), name=f"news-agent-{kind}-{job_id}")
        self._running[kind] = job_id
        self._tasks[job_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(job_id, None))
        return {"id": job_id, "status": "queued", "deduplicated": False}

    def cancel(self, job_id: str) -> dict:
        task = self._tasks.get(job_id)
        if task is None or task.done():
            return {"id": job_id, "status": "not_running", "cancelled": False}
        task.cancel()
        return {"id": job_id, "status": "cancelling", "cancelled": True}

    async def _run(self, job_id: str, kind: str, source: str) -> None:
        path = self.config_service.paths["runs"] / f"{job_id}.json"
        record = {"id": job_id, "kind": kind, "source": source, "status": "running",
                  "started_at": datetime.now(timezone.utc).isoformat()}
        self._save(path, record)
        logger = logging.getLogger(f"news_agent.{kind if kind in {'fetch', 'push'} else 'app'}")
        app_logger = logging.getLogger("news_agent.app")
        log_name = kind if kind in {"fetch", "push"} else "app"
        log_path = current_log_dir(self.config_service.paths["logs"]) / f"{log_name}.log"
        app_logger.info("job=%s kind=%s source=%s started log=%s", job_id, kind, source, log_path)
        try:
            async with self._locks[kind]:
                config = self.config_service.load()
                with log_path.open("a", encoding="utf-8", buffering=1) as stream:
                    stream.write(f"\n=== job {job_id} kind={kind} source={source} started {record['started_at']} ===\n")
                    stream.flush()
                    with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                        if kind == "fetch":
                            from src.main import run_fetch_job
                            await run_fetch_job(config)
                        elif kind == "push":
                            from src.main import run_push_job
                            await run_push_job(config)
                        elif kind == "run":
                            from src.main import run_fetch_job, run_push_job
                            logging.getLogger("news_agent.fetch").info("job=%s fetch stage started", job_id)
                            print(f"=== fetch stage started job={job_id} ===")
                            await run_fetch_job(config)
                            logging.getLogger("news_agent.fetch").info("job=%s fetch stage completed", job_id)
                            print(f"=== push stage started job={job_id} ===")
                            logging.getLogger("news_agent.push").info("job=%s push stage started", job_id)
                            await run_push_job(config)
                            logging.getLogger("news_agent.push").info("job=%s push stage completed", job_id)
                        else:
                            from src.sections.rss.section import run_rss_section
                            body, metadata, error = await run_rss_section(config)
                            if error:
                                raise RuntimeError(error)
                            record["preview"] = {"content": body, "metadata": metadata}
                    stream.write(f"=== job {job_id} completed {datetime.now(timezone.utc).isoformat()} ===\n")
                record["status"] = "succeeded"
                logger.info("job=%s kind=%s source=%s completed", job_id, kind, source)
        except asyncio.CancelledError:
            record["status"] = "cancelled"
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write(
                    f"=== job {job_id} cancelled {datetime.now(timezone.utc).isoformat()} ===\n"
                )
            logger.warning("job=%s kind=%s source=%s cancelled", job_id, kind, source)
            app_logger.warning("job=%s kind=%s source=%s cancelled", job_id, kind, source)
        except Exception as exc:
            record["status"] = "failed"
            record["error"] = str(exc)
            record["error_type"] = exc.__class__.__name__
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write(
                    f"=== job {job_id} failed {datetime.now(timezone.utc).isoformat()} "
                    f"{exc.__class__.__name__}: {exc} ===\n"
                )
            logger.exception("job=%s kind=%s source=%s failed", job_id, kind, source)
            app_logger.exception("job=%s kind=%s source=%s failed", job_id, kind, source)
        finally:
            record["finished_at"] = datetime.now(timezone.utc).isoformat()
            self._save(path, record)
            self._running.pop(kind, None)

    def get(self, job_id: str) -> dict | None:
        path = self.config_service.paths["runs"] / f"{job_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def recent(self, limit: int = 30) -> list[dict]:
        records = []
        for path in sorted(self.config_service.paths["runs"].glob("*.json"), reverse=True)[:limit]:
            try:
                records.append(json.loads(path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                continue
        return records

    @staticmethod
    def _save(path: Path, data: dict) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
