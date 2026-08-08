"""Single safe configuration boundary for CLI, Web API and MCP."""

import asyncio
import copy
import ipaddress
import json
import os
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
import feedparser
from croniter import croniter
from dotenv import dotenv_values

from src.runtime import PROJECT_ROOT, default_config_path, ensure_runtime_dirs
from src.config import merge_sources
from src.llm_protocol import LLM_PROTOCOLS, infer_llm_protocol
from src.source_categories import normalize_source_category


DEFAULT_DELIVERY_SCHEDULES = (
    {
        "id": "morning",
        "cron": "0 10 * * *",
        "max_items": 10,
    },
    {
        "id": "evening",
        "cron": "0 20 * * *",
        "max_items": 10,
    },
)


class ConfigError(ValueError):
    pass


def _deep_merge(base: dict, patch: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _source_id(url: str) -> str:
    import hashlib
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


class ConfigService:
    def __init__(self, config_path: Path | None = None):
        self.paths = ensure_runtime_dirs()
        self.config_path = (config_path or default_config_path()).expanduser().resolve()
        self._lock = asyncio.Lock()
        self._lock_path = self.config_path.with_name(f".{self.config_path.name}.lock")
        self._revision = 0

    @asynccontextmanager
    async def _transaction_lock(self):
        """Serialize config read-modify-write operations across local processes."""
        async with self._lock:
            deadline = time.monotonic() + 5
            while True:
                try:
                    fd = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.write(fd, str(os.getpid()).encode("ascii"))
                    os.close(fd)
                    break
                except FileExistsError:
                    try:
                        if time.time() - self._lock_path.stat().st_mtime > 60:
                            self._lock_path.unlink(missing_ok=True)
                            continue
                    except OSError:
                        pass
                    if time.monotonic() >= deadline:
                        raise ConfigError("configuration is being updated by another local process")
                    await asyncio.sleep(0.05)
            try:
                yield
            finally:
                self._lock_path.unlink(missing_ok=True)

    def _defaults(self) -> dict:
        return {
            "output_language": "en",
            "personal_preferences": "",
            "preferences": {
                "interests": [], "avoid": [], "source_weights": {},
                "language_preference": ["zh", "en"],
                "diversity": {"max_per_source": 2, "max_per_topic": 3},
            },
            "delivery": {"timezone": "Asia/Shanghai", "schedules": [],
                         "immediate": {"enabled": False, "threshold": 90, "daily_limit": 3}},
            "mode_settings": {
                "mode": "client",
                "server_url": "http://13.158.182.33:12301",
                "server_api_token_name": "processednews",
            },
            "storage": {"data_dir": str(self.paths["news_data"])},
            "log": {"retention_days": 30},
        }

    def _bootstrap(self) -> None:
        if self.config_path.exists():
            return
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        template = PROJECT_ROOT / "config.json.example"
        if not template.exists():
            raise ConfigError(f"configuration template not found: {template}")
        shutil.copyfile(template, self.config_path)

    def load(self) -> dict:
        self._bootstrap()
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"invalid JSON in {self.config_path}: {exc}") from exc
        has_personal_preferences = bool(str(raw.get("personal_preferences", "")).strip())
        has_delivery_schedules = (
            isinstance(raw.get("delivery"), dict)
            and "schedules" in raw["delivery"]
        )
        has_legacy_push_cron = (
            isinstance(raw.get("schedule"), dict)
            and "push_cron" in raw["schedule"]
        )
        config = _deep_merge(self._defaults(), raw)
        self._migrate_legacy(
            config,
            has_personal_preferences,
            has_delivery_schedules=has_delivery_schedules,
            has_legacy_push_cron=has_legacy_push_cron,
        )
        self.validate(config)
        llm = config.setdefault("llm", {})
        llm.setdefault(
            "protocol",
            infer_llm_protocol(llm.get("baseUrl", ""), llm.get("model", "")),
        )
        config["llm"]["output_language"] = config["output_language"]
        config["llm"]["personal_preferences"] = config["personal_preferences"]
        if not has_personal_preferences:
            # Persist the one-time move from the legacy environment variable
            # or structured preferences into the user-facing config field.
            self._write(config)
        return config

    def _migrate_legacy(
        self,
        config: dict,
        has_personal_preferences: bool,
        *,
        has_delivery_schedules: bool,
        has_legacy_push_cron: bool,
    ) -> None:
        delivery = config["delivery"]
        if not has_delivery_schedules and has_legacy_push_cron:
            old = config.get("schedule", {}).get("push_cron", [])
            delivery["schedules"] = [
                {"id": "morning" if index == 0 else f"push-{index + 1}", "cron": cron,
                 "max_items": 10}
                for index, cron in enumerate(old)
            ]
        elif not has_delivery_schedules and not has_legacy_push_cron:
            delivery["schedules"] = copy.deepcopy(list(DEFAULT_DELIVERY_SCHEDULES))
        for schedule in delivery.get("schedules", []):
            schedule.pop("sections", None)
        config["preferences"].setdefault("interests", [])
        config["preferences"].setdefault("avoid", [])
        if not has_personal_preferences:
            legacy_description = os.environ.get("PERSONAL_PREFERENCES", "").strip()
            if not legacy_description:
                legacy_description = str(dotenv_values(PROJECT_ROOT / ".env").get("PERSONAL_PREFERENCES") or "").strip()
            if legacy_description:
                config["personal_preferences"] = legacy_description
            else:
                preferences = config["preferences"]
                interests = preferences.get("interests", [])
                avoid = preferences.get("avoid", [])
                lines = []
                if interests:
                    lines.append(f"Interested in: {', '.join(interests)}")
                if avoid:
                    lines.append(f"Avoid: {', '.join(avoid)}")
                config["personal_preferences"] = "\n".join(lines)

    def validate(self, config: dict) -> None:
        if not isinstance(config.get("sources"), dict):
            raise ConfigError("sources must be an object")
        if not isinstance(config.get("personal_preferences", ""), str):
            raise ConfigError("personal_preferences must be a string")
        if len(config["personal_preferences"]) > 4_000:
            raise ConfigError("personal_preferences is too long")
        if config.get("output_language") not in {"en", "zh"}:
            raise ConfigError("output_language must be 'en' or 'zh'")
        mode_settings = config.get("mode_settings", {})
        if not isinstance(mode_settings, dict):
            raise ConfigError("mode_settings must be an object")
        if mode_settings.get("mode") not in {"standalone", "mix", "client"}:
            raise ConfigError("mode_settings.mode must be 'standalone', 'mix', or 'client'")
        server_url = urlparse(str(mode_settings.get("server_url", "")))
        if server_url.scheme not in {"http", "https"} or not server_url.netloc:
            raise ConfigError("mode_settings.server_url must be an http or https URL")
        llm = config.get("llm", {})
        if not isinstance(llm, dict):
            raise ConfigError("llm must be an object")
        if llm.get("protocol") is not None and llm["protocol"] not in LLM_PROTOCOLS:
            raise ConfigError(f"llm.protocol must be one of: {', '.join(LLM_PROTOCOLS)}")
        for source in config["sources"].get("add", []):
            self.validate_source(source)
        for schedule in config.get("delivery", {}).get("schedules", []):
            cron = schedule.get("cron", "")
            if not croniter.is_valid(cron):
                raise ConfigError(f"invalid cron expression: {cron}")
            if int(schedule.get("max_items", 1)) < 1:
                raise ConfigError("schedule max_items must be positive")
        immediate = config.get("delivery", {}).get("immediate", {})
        if not 0 <= int(immediate.get("threshold", 90)) <= 100:
            raise ConfigError("immediate threshold must be between 0 and 100")

    def validate_source(self, source: dict) -> None:
        title, url = str(source.get("title", "")).strip(), str(source.get("xmlUrl", "")).strip()
        if not title or not url:
            raise ConfigError("RSS source requires title and xmlUrl")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ConfigError("RSS source must use an http or https URL")
        host = parsed.hostname.lower()
        if host == "localhost" or host.endswith(".localhost"):
            raise ConfigError("localhost is not an allowed RSS source")
        try:
            address = ipaddress.ip_address(host)
            if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
                raise ConfigError("private network addresses are not allowed")
        except ValueError:
            # Do not pre-resolve hostnames here. Corporate proxies frequently map public
            # names to private proxy addresses, which would reject valid default feeds.
            # Explicit IP literals and the final redirected URL are still checked.
            pass

    async def verify_source(self, source: dict) -> dict:
        self.validate_source(source)
        url = source["xmlUrl"]
        timeout = aiohttp.ClientTimeout(total=12)
        try:
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                async with session.get(url, allow_redirects=True) as response:
                    final_url = str(response.url)
                    self.validate_source({"title": source["title"], "xmlUrl": final_url})
                    if response.status != 200:
                        raise ConfigError(f"feed returned HTTP {response.status}")
                    payload = await response.read()
        except aiohttp.ClientError as exc:
            raise ConfigError(f"could not fetch RSS source: {exc}") from exc
        parsed = feedparser.parse(payload)
        if parsed.bozo and not parsed.entries:
            raise ConfigError("response is not a readable RSS or Atom feed")
        return {"ok": True, "title": parsed.feed.get("title", source["title"]),
                "entries": len(parsed.entries), "url": final_url}

    def sources(self, config: dict | None = None) -> list[dict]:
        config = config or self.load()
        custom_urls = {item.get("xmlUrl") for item in config["sources"].get("add", [])}
        result = []
        for source in merge_sources(config["sources"]):
            item = copy.deepcopy(source)
            item["id"] = _source_id(item["xmlUrl"])
            item["kind"] = "custom" if item["xmlUrl"] in custom_urls else "builtin"
            result.append(item)
        return result

    async def update(self, patch: dict, actor: str = "api") -> tuple[dict, int]:
        async with self._transaction_lock():
            current = self.load()
            updated = _deep_merge(current, patch)
            self.validate(updated)
            self._write(updated)
            self._revision += 1
            self._audit(actor, "update_config", {"keys": sorted(patch.keys()), "revision": self._revision})
            return updated, self._revision

    async def add_source(self, source: dict, actor: str = "api") -> tuple[dict, int]:
        self.validate_source(source)
        category = normalize_source_category(
            source.get("category"),
            title=source.get("title", ""),
            url=source.get("xmlUrl", ""),
        )
        async with self._transaction_lock():
            current = self.load()
            items = current["sources"].setdefault("add", [])
            if any(item.get("xmlUrl") == source["xmlUrl"] for item in items):
                raise ConfigError("RSS source already exists")
            items.append({"title": source["title"].strip(), "xmlUrl": source["xmlUrl"].strip(),
                          "category": category})
            self.validate(current)
            self._write(current)
            self._revision += 1
            self._audit(actor, "add_source", {"url": source["xmlUrl"], "revision": self._revision})
            return current, self._revision

    async def update_source(self, source_id: str, patch: dict, actor: str = "api") -> tuple[dict, int]:
        async with self._transaction_lock():
            current = self.load()
            items = current["sources"].setdefault("add", [])
            source = next((item for item in items if _source_id(item.get("xmlUrl", "")) == source_id), None)
            clean_patch = {key: value for key, value in patch.items() if key in {"title", "xmlUrl", "category"}}
            if "category" in clean_patch:
                clean_patch["category"] = normalize_source_category(
                    clean_patch["category"],
                    title=clean_patch.get("title", source.get("title", "")),
                    url=clean_patch.get("xmlUrl", source.get("xmlUrl", "")),
                )
            source.update(clean_patch)
            self.validate(current)
            self._write(current)
            self._revision += 1
            self._audit(actor, "update_source", {"id": source_id, "revision": self._revision})
            return current, self._revision

    async def remove_source(self, source_id: str, actor: str = "api") -> tuple[dict, int]:
        async with self._transaction_lock():
            current = self.load()
            items = current["sources"].setdefault("add", [])
            remaining = [item for item in items if _source_id(item.get("xmlUrl", "")) != source_id]
            if len(remaining) == len(items):
                active = next((item for item in self.sources(current) if item["id"] == source_id), None)
                if active is not None:
                    blocks = current["sources"].setdefault("block", [])
                    if not any(item.get("xmlUrl") == active["xmlUrl"] for item in blocks):
                        blocks.append({"title": active["title"], "xmlUrl": active["xmlUrl"]})
                elif source_id in {"github_trending", "hackernews"}:
                    current.setdefault("sections", {}).setdefault(source_id, {})["enabled"] = False
                else:
                    from src.sections.signals.collector import signal_source_catalog
                    sig = next((s for s in signal_source_catalog() if s["id"] == source_id), None)
                    if sig is not None:
                        current.setdefault("sections", {}).setdefault("signals", {}).setdefault("sources", {})[source_id] = False
                    else:
                        raise ConfigError("RSS or signal source not found")
            else:
                current["sources"]["add"] = remaining
            self._write(current)
            self._revision += 1
            self._audit(actor, "remove_source", {"id": source_id, "revision": self._revision})
            return current, self._revision

    def _write(self, config: dict) -> None:
        config = copy.deepcopy(config)
        for schedule in config.get("delivery", {}).get("schedules", []):
            schedule.pop("sections", None)
        if isinstance(config.get("llm"), dict):
            config["llm"].pop("output_language", None)
            config["llm"].pop("personal_preferences", None)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="config-", suffix=".json", dir=self.config_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(config, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.config_path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _audit(self, actor: str, action: str, detail: dict[str, Any]) -> None:
        path = self.paths["logs"] / "audit.log"
        entry = {"time": datetime.now(timezone.utc).isoformat(), "actor": actor, "action": action, "detail": detail}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
