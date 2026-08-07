"""stdio MCP interface for the local News Agent control plane."""

import asyncio
import json
import logging

from src.app_logging import configure_logging
from src.app_logging import current_log_dir
from src.config_service import ConfigError, ConfigService
from src.jobs import JobExecutor


def create_mcp_server():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError("MCP support is not installed. Run `uv sync` to install project dependencies.") from exc

    config_service = ConfigService()
    config = config_service.load()
    configure_logging(config_service.paths["logs"], int(config.get("log", {}).get("retention_days", 30)))
    jobs = JobExecutor(config_service)
    logger = logging.getLogger("news_agent.mcp")
    mcp = FastMCP("News Agent")

    @mcp.tool()
    def get_status() -> dict:
        """Return local service configuration and active task status."""
        return {"config_path": str(config_service.config_path), "jobs": jobs.status(), "recent_jobs": jobs.recent(10)}

    @mcp.tool()
    def get_config_summary() -> dict:
        """Return preferences, delivery schedules, and custom RSS sources without secrets."""
        current = config_service.load()
        return {
            "output_language": current["output_language"],
            "personal_preferences": current["personal_preferences"],
            "preferences": current["preferences"],
            "delivery": current["delivery"],
            "sources": config_service.sources(current),
        }

    @mcp.tool()
    def list_sources() -> list[dict]:
        """List all active RSS sources, including built-in and custom sources."""
        return config_service.sources()

    @mcp.tool()
    async def verify_source(title: str, xml_url: str, category: str = "Custom") -> dict:
        """Verify an RSS or Atom source before adding it."""
        return await config_service.verify_source({"title": title, "xmlUrl": xml_url, "category": category})

    @mcp.tool()
    async def add_rss_source(title: str, xml_url: str, category: str = "Custom") -> dict:
        """Add a custom RSS source after URL validation. Verify first when possible."""
        _, revision = await config_service.add_source({"title": title, "xmlUrl": xml_url, "category": category}, "mcp")
        logger.info("added source url=%s revision=%s", xml_url, revision)
        return {"revision": revision, "message": "RSS source added"}

    @mcp.tool()
    async def remove_source(source_id: str, confirm: bool = False) -> dict:
        """Remove a custom RSS source. confirm must be true."""
        if not confirm:
            return {"error": "Set confirm=true to remove a source."}
        _, revision = await config_service.remove_source(source_id, "mcp")
        return {"revision": revision, "message": "RSS source removed"}

    @mcp.tool()
    async def set_preferences(interests: list[str], avoid: list[str] | None = None,
                              source_weights: dict[str, int] | None = None,
                              language_preference: list[str] | None = None) -> dict:
        """Replace structured content ranking preferences."""
        preferences = {"interests": interests, "avoid": avoid or [], "source_weights": source_weights or {},
                       "language_preference": language_preference or ["zh", "en"]}
        _, revision = await config_service.update({"preferences": preferences}, "mcp")
        return {"revision": revision, "preferences": preferences}

    @mcp.tool()
    async def set_output_language(language: str) -> dict:
        """Set the language for generated news pushes. Use 'zh' for Chinese or 'en' for English."""
        if language not in {"zh", "en"}:
            return {"error": "language must be 'zh' or 'en'"}
        config, revision = await config_service.update({"output_language": language}, "mcp")
        return {"revision": revision, "output_language": config["output_language"]}

    @mcp.tool()
    async def set_delivery_schedule(schedules: list[dict], timezone: str = "Asia/Shanghai") -> dict:
        """Set delivery cron schedules. Each item needs id, cron, max_items, and sections."""
        delivery = config_service.load()["delivery"]
        delivery["timezone"] = timezone
        delivery["schedules"] = schedules
        _, revision = await config_service.update({"delivery": delivery}, "mcp")
        return {"revision": revision, "message": "Schedules saved. A running local service reloads them within five seconds."}

    @mcp.tool()
    def run_fetch() -> dict:
        """Start a fetch task and return its ID."""
        return jobs.submit("fetch", "mcp")

    @mcp.tool()
    def run_push(confirm: bool = False) -> dict:
        """Start a real push task. confirm must be true because it can send messages."""
        if not confirm:
            return {"error": "Set confirm=true to send a push."}
        return jobs.submit("push", "mcp")

    @mcp.tool()
    def preview_digest() -> dict:
        """Generate a digest preview without sending it or marking links as sent."""
        return jobs.submit("preview", "mcp")

    @mcp.tool()
    def get_job(job_id: str) -> dict:
        """Return the current or completed job record."""
        return jobs.get(job_id) or {"error": "job not found"}

    @mcp.tool()
    def get_recent_logs(name: str = "app", lines: int = 100) -> list[str]:
        """Read recent portable application log lines."""
        if name not in {"app", "fetch", "push", "web", "mcp", "audit"}:
            return ["Unknown log name"]
        path = current_log_dir(config_service.paths["logs"]) / f"{name}.log"
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, min(lines, 1000)):]

    return mcp


def run_mcp() -> None:
    create_mcp_server().run(transport="stdio")
