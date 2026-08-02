"""Tests for the local configuration and task control plane."""

import json
from pathlib import Path

import pytest

from src.config_service import ConfigError, ConfigService
from src.environment_service import EnvironmentService
from src.jobs import JobExecutor


def _config(path: Path) -> Path:
    config = {
        "sources": {"base_opml": "missing.opml", "add": [], "block": [], "block_domains": []},
        "filter": {"min_score": 60, "hot_threshold": 90, "context_days": 2, "keep_days": 7},
        "schedule": {"fetch_interval_minutes": 60, "push_cron": ["0 8 * * *"], "timezone_hours": 8},
        "llm": {"provider": "test", "model": "test", "baseUrl": "https://example.com", "apiKeyName": "TEST"},
        "push": {},
    }
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_config_service_adds_source_and_creates_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWS_AGENT_DATA_DIR", str(tmp_path / "data"))
    service = ConfigService(_config(tmp_path / "config.json"))

    updated, revision = await service.add_source(
        {"title": "Example", "xmlUrl": "https://example.com/feed.xml", "category": "Test"}, "test"
    )

    assert revision == 1
    assert updated["sources"]["add"][0]["title"] == "Example"
    assert service.sources()[0]["kind"] == "custom"
    assert (service.paths["logs"] / "audit.log").exists()


@pytest.mark.asyncio
async def test_config_service_persists_personal_preference_description(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWS_AGENT_DATA_DIR", str(tmp_path / "data"))
    path = _config(tmp_path / "config.json")
    service = ConfigService(path)

    updated, revision = await service.update(
        {"personal_preferences": "Prioritize practical AI agent updates."}, "test"
    )

    assert revision == 1
    assert updated["personal_preferences"] == "Prioritize practical AI agent updates."
    assert json.loads(path.read_text(encoding="utf-8"))["personal_preferences"] == "Prioritize practical AI agent updates."


def test_config_service_rejects_local_source(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWS_AGENT_DATA_DIR", str(tmp_path / "data"))
    service = ConfigService(_config(tmp_path / "config.json"))
    with pytest.raises(ConfigError, match="localhost"):
        service.validate_source({"title": "Local", "xmlUrl": "http://localhost/feed"})


def test_environment_service_hides_values_and_updates_process_environment(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("TEST_NEWS_AGENT_SECRET=old-value\n", encoding="utf-8")
    template_path = tmp_path / ".env.example"
    template_path.write_text("TEST_NEWS_AGENT_SECRET=\nTEST_NEWS_AGENT_OPTIONAL=\n", encoding="utf-8")
    service = EnvironmentService(env_path)
    service.template_path = template_path

    status = service.status({"llm": {"apiKeyName": "TEST_NEWS_AGENT_SECRET"}, "push": {}, "sections": {}})
    assert {item["name"] for item in status["variables"]} == {
        "NEWS_AGENT_LOCAL_TOKEN", "TEST_NEWS_AGENT_OPTIONAL", "TEST_NEWS_AGENT_SECRET"
    }
    assert next(item for item in status["variables"] if item["name"] == "TEST_NEWS_AGENT_SECRET")["value"] == "old-value"

    service.update({"TEST_NEWS_AGENT_SECRET": "new-value"})
    assert "new-value" in env_path.read_text(encoding="utf-8")
    assert __import__("os").environ["TEST_NEWS_AGENT_SECRET"] == "new-value"
    monkeypatch.delenv("TEST_NEWS_AGENT_SECRET", raising=False)


@pytest.mark.asyncio
async def test_job_executor_deduplicates_running_job(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWS_AGENT_DATA_DIR", str(tmp_path / "data"))
    service = ConfigService(_config(tmp_path / "config.json"))
    jobs = JobExecutor(service)
    first = jobs.submit("preview", "test")
    second = jobs.submit("preview", "test")
    assert first["id"] == second["id"]
    assert second["deduplicated"] is True
    await jobs._tasks[first["id"]]
    record = jobs.get(first["id"])
    assert record["status"] in {"succeeded", "failed"}
