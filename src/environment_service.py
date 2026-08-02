"""Safe local management of the project's .env file."""

import os
import re
from pathlib import Path

from dotenv import dotenv_values, set_key, unset_key

from src.runtime import PROJECT_ROOT


_VARIABLE_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


class EnvironmentError(ValueError):
    pass


class EnvironmentService:
    """Expose .env metadata without exposing its values."""

    def __init__(self, path: Path | None = None):
        self.path = path or PROJECT_ROOT / ".env"
        self.template_path = PROJECT_ROOT / ".env.example"

    def status(self, config: dict) -> dict:
        values = self._read(self.path)
        names = set(self._read(self.template_path)) | set(values) | self._configured_names(config)
        names.discard("PERSONAL_PREFERENCES")
        variables = [
            {"name": name, "configured": bool(values.get(name)), "value": values.get(name) or ""}
            for name in sorted(names)
            if _VARIABLE_NAME.fullmatch(name)
        ]
        return {"path": str(self.path), "variables": variables}

    def update(self, values: dict[str, str]) -> dict:
        if not isinstance(values, dict):
            raise EnvironmentError("values must be an object")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for name, value in values.items():
            if not isinstance(name, str) or not _VARIABLE_NAME.fullmatch(name):
                raise EnvironmentError("environment variable names must use uppercase letters, numbers, and underscores")
            if not isinstance(value, str):
                raise EnvironmentError(f"value for {name} must be a string")
            if len(value) > 16_384:
                raise EnvironmentError(f"value for {name} is too long")
            if value:
                set_key(str(self.path), name, value, quote_mode="auto")
                os.environ[name] = value
            else:
                unset_key(str(self.path), name)
                os.environ.pop(name, None)
        return {"updated": sorted(values), "restart_recommended": "NEWS_AGENT_LOCAL_TOKEN" in values}

    @staticmethod
    def _read(path: Path) -> dict[str, str | None]:
        return dict(dotenv_values(path)) if path.exists() else {}

    @staticmethod
    def _configured_names(config: dict) -> set[str]:
        names = {"NEWS_AGENT_LOCAL_TOKEN"}
        llm_name = config.get("llm", {}).get("apiKeyName")
        if isinstance(llm_name, str):
            names.add(llm_name)
        for platform in config.get("push", {}).values():
            if isinstance(platform, dict):
                for key in ("apiKeyName", "tokenKeyName"):
                    name = platform.get(key)
                    if isinstance(name, str):
                        names.add(name)
        sections = config.get("sections", {})
        for section, key in (("github_trending", "tokenName"), ("hackernews", "jinaTokenName")):
            name = sections.get(section, {}).get(key)
            if isinstance(name, str):
                names.add(name)
        return names
