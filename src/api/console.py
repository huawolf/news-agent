"""The operations console shared by master and slaver (§8.2, §8.3).

Both instances render the same page and call the same paths. The only
per-instance differences live in this module:

* **Access.** The guard is injected by :func:`build_console_router`: master
  passes ``admin_identity`` (verified administrator JWT), slaver passes
  :func:`local_console_guard` (loopback only, no proxy headers).
* **Row owner.** master writes ``public`` rows; slaver writes its per-instance
  owner (``user:<id>`` / placeholder).
* **Fetch cycle.** ``fetch-once`` calls ``app.state.fetch_runner``, which each
  app wires to its own pipeline in ``create_app``.
"""
from __future__ import annotations

import asyncio
import base64
import io
import ipaddress
import json
import logging
import os
import re
import secrets
import tempfile
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import segno

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from src.api.serializers import feed_source_item, source_item
from src.config import PROJECT_ROOT, MAX_PREFERENCE_CHARS, get_mode, get_server_url
from src.db.bootstrap import PLACEHOLDER_OWNER
from src.db.repositories import (
    create_source,
    delete_source,
    get_setting,
    get_source,
    list_sources,
    set_setting,
)
from src.db.session import session_scope
from src.llm_protocol import LLM_PROTOCOLS
from src.runtime import default_config_path, write_config_value


logger = logging.getLogger(__name__)

#: master stores one shared row set for every per-user concept (§4.1).
PUBLIC_OWNER = "public"

#: Environment-variable name holding the Feishu bot webhook when the config
#: section does not name one.
FEISHU_DEFAULT_KEY = "FEISHU_WEBHOOK_URL"

#: Environment-variable name holding the Telegram bot token by default.
TELEGRAM_DEFAULT_KEY = "TELEGRAM_BOT_TOKEN"

TELEGRAM_API_URL = "https://api.telegram.org"

#: How long a ``/start`` binding link stays usable.
TELEGRAM_BIND_TTL_MINUTES = 15

#: ``-100…`` for groups/channels, ``@name`` for public channels, plain digits
#: for private chats.
TELEGRAM_CHAT_ID_PATTERN = re.compile(r"@[A-Za-z0-9_]{4,64}|-?[0-9]{4,20}")

#: Fetch cycle interval when ``schedule.fetch_interval_minutes`` is absent.
DEFAULT_FETCH_INTERVAL_MINUTES = 60

#: Timezone used when no browser timezone has been saved yet.
DEFAULT_DELIVERY_TIMEZONE = "Asia/Shanghai"

#: Default console plans; cron schedules are stored in this order and edited
#: as weekday names plus ``HH:MM``.
DEFAULT_PUSH_SCHEDULES = (
    {"days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], "time": "09:00"},
    {"days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], "time": "17:00"},
)
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
CRON_WEEKDAY_NAMES = {"mon": "mon", "tue": "tue", "wed": "wed", "thu": "thu", "fri": "fri", "sat": "sat", "sun": "sun"}

#: Digest section caps (热点资讯 / 关注资讯) fallback values.
DEFAULT_PUSH_LIMITS = {"hot": 10, "watchlist": 10, "min_score": 80, "max_per_topic": 3}

#: Upper bound for a push time or a per-section digest cap.
MAX_PUSH_ITEMS = 100

TIME_OF_DAY_PATTERN = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


def _as_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _cron_to_schedule(cron: str) -> dict | None:
    """Decode only simple minute/hour/weekdays cron schedules."""
    parts = str(cron or "").split()
    if len(parts) != 5 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    minute, hour = int(parts[0]), int(parts[1])
    if not (0 <= minute < 60 and 0 <= hour < 24) or parts[2:4] != ["*", "*"]:
        return None
    weekday_field = parts[4].lower()
    if weekday_field == "*":
        days = list(WEEKDAYS)
    else:
        day_values = set()
        for token in weekday_field.split(","):
            if token in CRON_WEEKDAY_NAMES:
                day_values.add(token)
            elif token.isdigit() and 0 <= int(token) <= 7:
                day_values.add(WEEKDAYS[(int(token) - 1) % 7])
            else:
                return None
        if not day_values:
            return None
        days = [day for day in WEEKDAYS if day in day_values]
    return {"days": days, "time": f"{hour:02d}:{minute:02d}"}


def _schedule_to_cron(days: list[str], value: str) -> str:
    hour, minute = value.split(":")
    weekdays = ",".join(day for day in WEEKDAYS if day in days)
    return f"{int(minute)} {int(hour)} * * {weekdays}"


def _delivery_state(config: dict) -> dict:
    """Normalise the fetch/push settings the console edits (§5.3, §8.2)."""
    schedule = config.get("schedule") or {}
    delivery = config.get("delivery") or {}
    limits = delivery.get("push_limits") or {}
    stored_schedules = delivery.get("schedules")
    if stored_schedules is None:
        push_schedules = [dict(item) for item in DEFAULT_PUSH_SCHEDULES]
    else:
        push_schedules = [
            parsed
            for item in stored_schedules
            if isinstance(item, dict)
            and (parsed := _cron_to_schedule(item.get("cron"))) is not None
        ] if isinstance(stored_schedules, list) else []
    return {
        "fetch_interval_minutes": _as_int(
            schedule.get("fetch_interval_minutes"), DEFAULT_FETCH_INTERVAL_MINUTES
        ),
        "timezone": str(delivery.get("timezone") or DEFAULT_DELIVERY_TIMEZONE),
        "push_schedules": push_schedules,
        "push_limits": {
            "hot": _as_int(limits.get("hot"), DEFAULT_PUSH_LIMITS["hot"]),
            "watchlist": _as_int(limits.get("watchlist"), DEFAULT_PUSH_LIMITS["watchlist"]),
            "min_score": _as_float(limits.get("min_score"), DEFAULT_PUSH_LIMITS["min_score"]),
            "max_per_topic": _as_int(limits.get("max_per_topic"), 3),
        },
    }


def _feishu_config(request: Request) -> dict:
    return dict((request.app.state.config.get("push") or {}).get("feishu") or {})


def _telegram_config(request: Request) -> dict:
    return dict((request.app.state.config.get("push") or {}).get("telegram") or {})


def _public_base_url(request: Request) -> str:
    """The public origin (gateway prefix included) this instance is reachable at.

    Needed to register the Telegram webhook: Telegram must be able to call us
    back, so we volunteer the address the browser just used, honouring the
    Caddy ``X-Forwarded-*`` headers instead of trusting a hardcoded domain.
    """
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http").split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    prefix = request.headers.get("x-forwarded-prefix", "").strip()
    if not host or "/" in host or "\\" in host:
        return ""
    if not prefix.startswith("/") or prefix.startswith("//") or "\\" in prefix or any(char in prefix for char in "?#\"'<>\r\n"):
        prefix = ""
    return f"{proto}://{host}{prefix.rstrip('/')}"


def _qr_data_uri(value: str) -> str:
    """An inline SVG QR code, so the page needs no QR library and no network."""
    if not value:
        return ""
    buffer = io.BytesIO()
    segno.make(value).save(
        buffer, kind="svg", scale=8, border=2, dark="#111111", light=None, xmldecl=False, svgns=True
    )
    return "data:image/svg+xml;base64," + base64.b64encode(buffer.getvalue()).decode()


async def _telegram_api(token: str, method: str, payload: dict | None = None) -> dict:
    """Call one Bot API method; never raises so callers can report the reason."""
    import aiohttp

    url = f"{TELEGRAM_API_URL}/bot{token}/{method}"
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(url, json=payload or {}) as response:
                data = await response.json(content_type=None)
    except Exception as exc:
        logger.warning("telegram %s failed: %s", method, exc)
        return {"ok": False, "description": "无法连接 Telegram"}
    return data if isinstance(data, dict) else {"ok": False, "description": "Telegram 返回异常"}


async def _send_telegram_message(token: str, chat_id: str, text: str) -> None:
    from src.push.telegram import TelegramPlatform

    platform = TelegramPlatform({"chatId": chat_id, "request_timeout": 15})
    platform.bot_token = token
    try:
        await platform.send(text)
    except Exception as exc:
        logger.warning("telegram reply failed: %s", exc)


def _store_telegram_chat_id(request: Request, chat_id: str) -> None:
    """Persist a chat id discovered through the binding flow, without touching the switch."""
    current = _telegram_config(request)
    key_name = str(current.get("apiKeyName") or TELEGRAM_DEFAULT_KEY)
    _patch_config_section("push", {"telegram": {**current, "apiKeyName": key_name, "chatId": chat_id}})
    config = request.app.state.config
    push = config.setdefault("push", {})
    push["telegram"] = {**(push.get("telegram") or {}), "apiKeyName": key_name, "chatId": chat_id}


def is_master(request: Request) -> bool:
    return get_mode(request.app.state.config) == "master"


def require_local_console(request: Request) -> None:
    """Slaver's console is loopback-only and refuses proxy headers."""
    if any(request.headers.get(name) for name in ("forwarded", "x-forwarded-for", "x-real-ip", "x-forwarded-host")):
        raise HTTPException(status_code=403, detail="local console only")
    host = request.client.host if request.client else ""
    try:
        if not ipaddress.ip_address(host).is_loopback:
            raise HTTPException(status_code=403, detail="local console only")
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="local console only") from exc


def local_console_guard(request: Request) -> None:
    """Slaver's console guard, expressed as a FastAPI dependency."""
    require_local_console(request)


def _owner(request: Request) -> str:
    if is_master(request):
        return PUBLIC_OWNER
    owner = str(request.app.state.config.get("owner") or "")
    if owner:
        return owner
    if not get_server_url(request.app.state.config):
        return PLACEHOLDER_OWNER
    with session_scope() as session:
        user_id = get_setting(session, "user_id", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="login required")
    return f"user:{user_id}"


def _validate_endpoint(value: str) -> None:
    parsed = urlsplit(value)
    if (
        len(value) > 2048
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise HTTPException(status_code=400, detail="invalid endpoint URL")


def _validate_rss_payload(payload: dict) -> tuple[str, str]:
    title = str(payload.get("title") or "").strip()
    xml_url = str(payload.get("xml_url") or "").strip()
    parsed = urlsplit(xml_url)
    if not title or len(title) > 256:
        raise HTTPException(status_code=400, detail="RSS title is required")
    if (
        len(xml_url) > 2048
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise HTTPException(status_code=400, detail="valid RSS URL is required")
    return title, xml_url


def _atomic_write_text(path: Path, content: str, mode: int) -> None:
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
        if os.name != "nt":
            temporary_path.chmod(mode)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _write_env_value(key_name: str, value: str, *, clear: bool) -> str:
    """Insert / replace / drop one ``KEY=value`` line in the project ``.env``.

    An empty ``value`` without ``clear`` means **keep whatever is stored**: the
    console forms submit an empty string whenever the user did not re-type a
    masked secret, so treating that as a deletion would silently wipe the key
    from ``.env`` (it only survives until the next restart).

    Returns the previous file content so a caller can roll back when a later
    write in the same request fails.
    """
    env_path = PROJECT_ROOT / ".env"
    content = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    lines = content.splitlines(keepends=True)
    matcher = re.compile(rf"^(?:export\s+)?{re.escape(key_name)}\s*=")
    if clear or not value:
        replacement = None
    else:
        replacement = f"{key_name}={json.dumps(value, ensure_ascii=False)}\n"
    keep_existing = not clear and not value
    found = False
    updated = []
    for line in lines:
        if not matcher.match(line):
            updated.append(line)
        elif keep_existing:
            updated.append(line)
            found = True
        elif not found:
            found = True
            if replacement is not None:
                updated.append(replacement)
        # a second occurrence of the same key is collapsed into the first
    if not found and replacement is not None:
        if updated and not updated[-1].endswith(("\n", "\r")):
            updated[-1] += "\n"
        updated.append(replacement)
    if updated or env_path.exists():
        _atomic_write_text(env_path, "".join(updated), 0o600)
    return content


def _patch_config_sections(sections: dict) -> None:
    """Merge several top-level ``section`` objects into ``config.json`` at once.

    A single read-modify-write keeps a multi-section save (the delivery settings
    span ``schedule`` and ``delivery``) all-or-nothing, instead of leaving the
    file half updated when the second write fails.
    """
    path = default_config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    for section, values in sections.items():
        merged = dict(data.get(section) or {})
        merged.update(values)
        data[section] = merged
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n", mode)


def _patch_config_section(section: str, values: dict) -> None:
    """Merge ``values`` into a top-level ``section`` object of ``config.json``."""
    _patch_config_sections({section: values})


def _save_llm_config(
    request: Request, model: str, base_url: str, protocol: str, key_name: str,
    key: str, *, clear_key: bool,
) -> None:
    config = request.app.state.config
    config_path = default_config_path()
    try:
        saved_config = json.loads(config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        saved_config = config
    llm = dict(saved_config.get("llm") or {})
    llm.update({"model": model, "baseUrl": base_url, "protocol": protocol, "apiKeyName": key_name})
    saved_config["llm"] = llm
    config_content = json.dumps(saved_config, ensure_ascii=False, indent=2) + "\n"

    env_path = PROJECT_ROOT / ".env"
    env_existed = env_path.exists()
    original_env = _write_env_value(key_name, key, clear=clear_key)
    env_changed = bool(original_env or env_existed)
    config_mode = config_path.stat().st_mode & 0o777 if config_path.exists() else 0o600
    try:
        _atomic_write_text(config_path, config_content, config_mode)
    except Exception:
        if env_changed:
            try:
                if env_existed:
                    _atomic_write_text(env_path, original_env, 0o600)
                else:
                    env_path.unlink(missing_ok=True)
            except OSError:
                logger.exception("failed to roll back local LLM environment file")
        raise
    config["llm"] = {**(config.get("llm") or {}), **llm}
    if clear_key:
        os.environ.pop(key_name, None)
    elif key:
        os.environ[key_name] = key


async def _run_fetch_task(app) -> None:
    from dataclasses import asdict, is_dataclass

    try:
        runner = getattr(app.state, "fetch_runner", None)
        if runner is None:
            raise RuntimeError("fetch runner not ready")
        report = await runner()
        app.state.fetch_state = {
            "status": "completed",
            "started_at": app.state.fetch_state.get("started_at"),
            "report": asdict(report) if is_dataclass(report) else report,
        }
    except Exception:
        logger.exception("manual fetch cycle failed")
        app.state.fetch_state = {
            "status": "failed",
            "started_at": app.state.fetch_state.get("started_at"),
            "report": None,
            "message": "抓取失败，请检查本地日志。",
        }
    finally:
        app.state.fetch_lock.release()


async def extract_watchlist_assets(request: Request, text: str) -> list[dict]:
    """Recognise all explicit assets from pasted text via the LLM.

    Shared by master and slaver: the console no longer asks for an asset class
    up front. Each caller keeps its own ownership and persistence, because the
    watchlist owner differs per instance.
    """
    llm_config = request.app.state.config.get("llm") or {}
    key_name = str(llm_config.get("apiKeyName") or "")
    if not str(llm_config.get("model") or "").strip() or not os.environ.get(key_name, "").strip():
        raise HTTPException(status_code=400, detail="请先在控制台配置大模型和 API Key")

    from src.llm import recognize_watchlist_assets

    try:
        parsed = await recognize_watchlist_assets(text, llm_config)
    except Exception as exc:
        logger.exception("watchlist asset recognition failed")
        raise HTTPException(status_code=502, detail="资产识别失败，请检查大模型配置") from exc
    if not parsed:
        raise HTTPException(status_code=422, detail="未能从文字中识别出资产")
    return parsed


def build_console_router(guard) -> APIRouter:
    """The console routes, guarded by ``guard`` (admin JWT on master, loopback on slaver)."""
    router = APIRouter()

    @router.get("/api/sources")
    def sources(request: Request, _: None = Depends(guard)) -> dict:
        with session_scope() as session:
            owner = _owner(request)
            return {"items": [source_item(row) for row in list_sources(session, owner, kinds=["rss"])], "owner": owner}


    @router.get("/api/sources/feed")
    def feed_sources(request: Request, _: None = Depends(guard)) -> dict:
        with session_scope() as session:
            owner = _owner(request)
            rows = list_sources(session, owner, kinds=["feed"])
            return {"items": [feed_source_item(row) for row in rows], "owner": owner}


    @router.post("/api/sources")
    def create_rss_source(request: Request, payload: dict = Body(...), _: None = Depends(guard)) -> dict:
        title, xml_url = _validate_rss_payload(payload)
        enabled = payload.get("enabled", True)
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=400, detail="enabled must be a boolean")
        key = sha256(xml_url.encode("utf-8")).hexdigest()[:32]
        with session_scope() as session:
            owner = _owner(request)
            if get_source(session, owner, "rss", key) is not None:
                raise HTTPException(status_code=409, detail="RSS URL already exists")
            row = create_source(session, owner, "rss", key, title=title, xml_url=xml_url, enabled=enabled)
            session.flush()
            return source_item(row)


    @router.put("/api/sources/{kind}/{key}")
    def update_source(request: Request, kind: str, key: str, payload: dict = Body(...), _: None = Depends(guard)) -> dict:
        if kind != "rss":
            raise HTTPException(status_code=400, detail="only RSS sources can be edited here")
        if "enabled" in payload and not isinstance(payload["enabled"], bool):
            raise HTTPException(status_code=400, detail="enabled must be a boolean")
        with session_scope() as session:
            owner = _owner(request)
            row = get_source(session, owner, kind, key)
            if row is None:
                title, xml_url = _validate_rss_payload(payload)
                new_key = sha256(xml_url.encode("utf-8")).hexdigest()[:32]
                if get_source(session, owner, kind, new_key) is not None:
                    raise HTTPException(status_code=409, detail="RSS URL already exists")
                row = create_source(session, owner, kind, new_key, title=title, xml_url=xml_url, enabled=payload.get("enabled", True))
            else:
                title, xml_url = _validate_rss_payload({
                    "title": payload.get("title", row.title),
                    "xml_url": payload.get("xml_url", row.xml_url),
                })
                new_key = sha256(xml_url.encode("utf-8")).hexdigest()[:32]
                if new_key != row.key:
                    if get_source(session, owner, kind, new_key) is not None:
                        raise HTTPException(status_code=409, detail="RSS URL already exists")
                    row.key = new_key
                    row.etag = ""
                    row.last_modified = ""
                row.title = title
                row.xml_url = xml_url
                if "enabled" in payload:
                    row.enabled = payload["enabled"]
            session.flush()
            return source_item(row)


    @router.delete("/api/sources/{kind}/{key}")
    def remove_source(request: Request, kind: str, key: str, _: None = Depends(guard)) -> dict:
        if kind != "rss":
            raise HTTPException(status_code=400, detail="only RSS sources can be deleted here")
        with session_scope() as session:
            return {"deleted": delete_source(session, _owner(request), kind, key)}


    @router.get("/api/console/llm")
    def get_console_llm(request: Request, _: None = Depends(guard)) -> dict:
        llm = request.app.state.config.get("llm") or {}
        key_name = str(llm.get("apiKeyName") or "")
        key = os.environ.get(key_name, "") if key_name else ""
        return {
            "model": str(llm.get("model") or ""),
            "baseUrl": str(llm.get("baseUrl") or ""),
            "protocol": str(llm.get("protocol") or "openai_chat"),
            "apiKeyName": key_name,
            # Returned only to the console so it can show the current key masked with
            # a reveal toggle; no other endpoint echoes it back.
            "apiKey": key,
            "key_configured": bool(key.strip()),
        }


    @router.put("/api/console/llm")
    def put_console_llm(request: Request, payload: dict = Body(...), _: None = Depends(guard)) -> dict:
        model = str(payload.get("model") or "").strip()
        base_url = str(payload.get("baseUrl") or "").strip()
        protocol = str(payload.get("protocol") or "")
        current_llm = request.app.state.config.get("llm") or {}
        key_name = str(payload.get("apiKeyName") or current_llm.get("apiKeyName") or "OPENAI_API_KEY").strip()
        key = str(payload.get("apiKey") or "")
        if not model or len(model) > 200:
            raise HTTPException(status_code=400, detail="invalid model")
        _validate_endpoint(base_url)
        if protocol not in LLM_PROTOCOLS:
            raise HTTPException(status_code=400, detail="invalid protocol")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", key_name):
            raise HTTPException(status_code=400, detail="invalid key environment variable name")
        if "\n" in key or "\r" in key or "\x00" in key:
            raise HTTPException(status_code=400, detail="invalid key")
        _save_llm_config(request, model, base_url, protocol, key_name, key,
                         clear_key=bool(payload.get("clear_key", False)))
        return {"saved": True, "key_configured": bool(os.environ.get(key_name, "").strip())}


    @router.post("/api/console/llm/test")
    async def test_console_llm(request: Request, payload: dict = Body(default={}), _: None = Depends(guard)) -> dict:
        current = request.app.state.config.get("llm") or {}
        model = str(payload.get("model") or current.get("model") or "").strip()
        base_url = str(payload.get("baseUrl") or current.get("baseUrl") or "").strip()
        protocol = str(payload.get("protocol") or current.get("protocol") or "openai_chat")
        key_name = str(payload.get("apiKeyName") or current.get("apiKeyName") or "OPENAI_API_KEY").strip()
        key = str(payload.get("apiKey") or "").strip() or os.environ.get(key_name, "").strip()
        if not model or not key_name or not key or protocol not in LLM_PROTOCOLS:
            raise HTTPException(status_code=400, detail="complete model settings and API key are required")
        _validate_endpoint(base_url)
        try:
            from src.llm import check_llm_available

            result = await check_llm_available(
                {**current, "model": model, "baseUrl": base_url, "protocol": protocol, "apiKeyName": key_name},
                timeout_seconds=15,
                api_key=key,
            )
            return {"ok": True, "message": result[:120]}
        except Exception:
            return {"ok": False, "message": "连接测试失败，请检查 endpoint、模型和 API Key。"}


    @router.get("/api/console/preferences")
    def get_console_preferences(request: Request, _: None = Depends(guard)) -> dict:
        return {"preferences": str(request.app.state.config.get("personal_preferences") or "")}


    @router.put("/api/console/preferences")
    def put_console_preferences(request: Request, payload: dict = Body(...), _: None = Depends(guard)) -> dict:
        """Save the instance's natural-language ranking preference.

        The text is injected into the scoring prompt's ``{preferences}`` slot, so it
        is written back to ``config.json`` to survive a restart and mirrored into the
        in-memory ``llm`` section the scoring path reads.
        """
        text = str(payload.get("preferences") or "").strip()
        if len(text) > MAX_PREFERENCE_CHARS:
            raise HTTPException(status_code=400, detail="preferences too long")
        write_config_value("personal_preferences", text, default_config_path())
        config = request.app.state.config
        config["personal_preferences"] = text
        config.setdefault("llm", {})["personal_preferences"] = text
        return {"preferences": text}


    @router.get("/api/console/delivery")
    def get_console_delivery(request: Request, _: None = Depends(guard)) -> dict:
        return _delivery_state(request.app.state.config)


    @router.put("/api/console/delivery")
    def put_console_delivery(request: Request, payload: dict = Body(...), _: None = Depends(guard)) -> dict:
        """Save fetch timing and editable weekday push schedules atomically.

        The console submits each schedule as weekdays plus ``HH:MM``; the
        delivery section stores APScheduler cron in the selected timezone.
        After the atomic config write, the running scheduler is rebuilt so the
        change takes effect without a restart.
        """
        interval = _as_int(payload.get("fetch_interval_minutes"))
        if interval is None or not (1 <= interval <= 1440):
            raise HTTPException(status_code=400, detail="invalid fetch interval")
        push_limits = payload.get("push_limits") or {}
        if not isinstance(push_limits, dict):
            raise HTTPException(status_code=400, detail="invalid push limits")
        current_delivery = dict(request.app.state.config.get("delivery") or {})
        current_limits = dict(current_delivery.get("push_limits") or {})
        hot = _as_int(push_limits.get("hot"))
        watchlist = _as_int(push_limits.get("watchlist"))
        # ``min_score`` is optional on the wire: a console page that predates
        # the field must keep working, and the stored value survives.
        min_score = _as_float(
            push_limits.get("min_score"),
            _as_float(current_limits.get("min_score"), DEFAULT_PUSH_LIMITS["min_score"]),
        )
        max_per_topic = _as_int(
            push_limits.get("max_per_topic"),
            _as_int(current_limits.get("max_per_topic"), DEFAULT_PUSH_LIMITS["max_per_topic"]),
        )
        if (
            hot is None
            or watchlist is None
            or min_score is None
            or max_per_topic is None
            or not (0 <= hot <= MAX_PUSH_ITEMS)
            or not (0 <= watchlist <= MAX_PUSH_ITEMS)
            or not (0 <= min_score <= 100)
            or not (0 <= max_per_topic <= MAX_PUSH_ITEMS)
        ):
            raise HTTPException(status_code=400, detail="invalid push limits")
        timezone_name = str(payload.get("timezone") or "").strip() or DEFAULT_DELIVERY_TIMEZONE
        try:
            ZoneInfo(timezone_name)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="invalid timezone") from exc
        push_schedules = payload.get("push_schedules")
        if not isinstance(push_schedules, list):
            raise HTTPException(status_code=400, detail="invalid push schedules")
        normalized_schedules = []
        for index, item in enumerate(push_schedules, start=1):
            if not isinstance(item, dict):
                raise HTTPException(status_code=400, detail=f"invalid push schedule: {index}")
            days = item.get("days")
            if not isinstance(days, list) or not days or any(not isinstance(day, str) or day not in WEEKDAYS for day in days) or len(set(days)) != len(days):
                raise HTTPException(status_code=400, detail=f"invalid push schedule days: {index}")
            value = item.get("time")
            if not isinstance(value, str) or not TIME_OF_DAY_PATTERN.fullmatch(value):
                raise HTTPException(status_code=400, detail=f"invalid push schedule time: {index}")
            normalized_schedules.append({"days": [day for day in WEEKDAYS if day in days], "time": value})

        existing = [
            item for item in current_delivery.get("schedules", []) if isinstance(item, dict)
        ]
        schedules = []
        for index, item in enumerate(normalized_schedules, start=1):
            # Keep per-row delivery options such as locale and max_items while
            # assigning new stable IDs and the edited time/day expression.
            entry = dict(existing[index - 1]) if index <= len(existing) else {}
            entry["id"] = f"push-{index}"
            entry["cron"] = _schedule_to_cron(item["days"], item["time"])
            schedules.append(entry)
        delivery = {
            **current_delivery,
            "timezone": timezone_name,
            "schedules": schedules,
            "push_limits": {
                **current_limits,
                "hot": hot,
                "watchlist": watchlist,
                "min_score": min_score,
                "max_per_topic": max_per_topic,
            },
        }
        _patch_config_sections(
            {
                "schedule": {"fetch_interval_minutes": interval},
                "delivery": delivery,
            }
        )
        config = request.app.state.config
        config["schedule"] = {**(config.get("schedule") or {}), "fetch_interval_minutes": interval}
        config["delivery"] = delivery
        apply_schedule = getattr(request.app.state, "apply_schedule", None)
        if callable(apply_schedule):
            apply_schedule()
        return _delivery_state(config)


    @router.get("/api/console/feishu")
    def get_console_feishu(request: Request, _: None = Depends(guard)) -> dict:
        key_name = str(_feishu_config(request).get("apiKeyName") or FEISHU_DEFAULT_KEY)
        webhook = os.environ.get(key_name, "") if key_name else ""
        return {
            "enabled": bool(_feishu_config(request).get("enabled", False)),
            "apiKeyName": key_name,
            # Returned only to the console so the page can show it masked with a
            # reveal toggle; the PUT response and every other endpoint omit it.
            "webhook": webhook,
            "configured": bool(webhook.strip()),
        }

    @router.put("/api/console/feishu")
    def put_console_feishu(request: Request, payload: dict = Body(...), _: None = Depends(guard)) -> dict:
        """Save the Feishu push switch and webhook.

        The switch lives in ``config.json`` (``push.feishu.enabled``), the
        webhook only in the project ``.env`` under ``push.feishu.apiKeyName``.
        """
        enabled = payload.get("enabled", False)
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=400, detail="enabled must be a boolean")
        current = _feishu_config(request)
        key_name = str(current.get("apiKeyName") or FEISHU_DEFAULT_KEY)
        webhook = str(payload.get("webhook") or "").strip()
        if webhook and any(char in webhook for char in "\r\n\x00"):
            raise HTTPException(status_code=400, detail="invalid webhook")
        if webhook:
            _validate_endpoint(webhook)
        original_env = _write_env_value(key_name, webhook, clear=bool(payload.get("clear_webhook", False)))
        try:
            _patch_config_section("push", {"feishu": {**current, "enabled": enabled, "apiKeyName": key_name}})
        except Exception:
            try:
                _atomic_write_text(PROJECT_ROOT / ".env", original_env, 0o600)
            except OSError:
                logger.exception("failed to roll back the Feishu webhook environment file")
            raise
        if payload.get("clear_webhook"):
            os.environ.pop(key_name, None)
        elif webhook:
            os.environ[key_name] = webhook
        config = request.app.state.config
        push = config.setdefault("push", {})
        push["feishu"] = {**(push.get("feishu") or {}), "enabled": enabled, "apiKeyName": key_name}
        return {"saved": True, "configured": bool(os.environ.get(key_name, "").strip())}

    @router.post("/api/console/feishu/test")
    async def test_console_feishu(request: Request, payload: dict = Body(default={}), _: None = Depends(guard)) -> dict:
        key_name = str(payload.get("apiKeyName") or _feishu_config(request).get("apiKeyName") or FEISHU_DEFAULT_KEY)
        webhook = str(payload.get("webhook") or "").strip() or os.environ.get(key_name, "").strip()
        if not webhook:
            raise HTTPException(status_code=400, detail="complete the webhook URL first")
        _validate_endpoint(webhook)

        from src.push.feishu import FeishuPlatform

        platform = FeishuPlatform({"apiKeyName": key_name, "request_timeout": 15})
        platform.webhook_url = webhook
        try:
            await platform.send("News Agent 连通测试：飞书 Webhook 配置成功。", "News Agent")
            return {"ok": True, "message": "测试消息已发送，请查看飞书群。"}
        except Exception as exc:
            logger.warning("feishu test push failed: %s", exc)
            return {"ok": False, "message": "发送失败，请检查 Webhook 地址。"}

    @router.get("/api/console/telegram")
    def get_console_telegram(request: Request, _: None = Depends(guard)) -> dict:
        current = _telegram_config(request)
        key_name = str(current.get("apiKeyName") or TELEGRAM_DEFAULT_KEY)
        token = os.environ.get(key_name, "") if key_name else ""
        chat_id = str(current.get("chatId") or "")
        return {
            "enabled": bool(current.get("enabled", False)),
            "apiKeyName": key_name,
            # Returned only to the console so the page can show it masked with a
            # reveal toggle; the PUT response and every other endpoint omit it.
            "botToken": token,
            "chatId": chat_id,
            "configured": bool(token.strip() and chat_id.strip()),
        }

    @router.put("/api/console/telegram")
    def put_console_telegram(request: Request, payload: dict = Body(...), _: None = Depends(guard)) -> dict:
        """Save the Telegram push switch, bot token and chat id.

        The switch and chat id live in ``config.json``
        (``push.telegram.enabled`` / ``push.telegram.chatId``); the bot token
        only in the project ``.env`` under ``push.telegram.apiKeyName``.
        """
        enabled = payload.get("enabled", False)
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=400, detail="enabled must be a boolean")
        current = _telegram_config(request)
        key_name = str(current.get("apiKeyName") or TELEGRAM_DEFAULT_KEY)
        token = str(payload.get("botToken") or "").strip()
        chat_id = str(payload.get("chatId", current.get("chatId") or "")).strip()
        if token and (any(char in token for char in " \t\r\n\x00") or ":" not in token or len(token) > 200):
            raise HTTPException(status_code=400, detail="invalid bot token")
        if chat_id and not TELEGRAM_CHAT_ID_PATTERN.fullmatch(chat_id):
            raise HTTPException(status_code=400, detail="invalid chat id")
        original_env = _write_env_value(key_name, token, clear=bool(payload.get("clear_token", False)))
        try:
            _patch_config_section(
                "push",
                {"telegram": {**current, "enabled": enabled, "apiKeyName": key_name, "chatId": chat_id}},
            )
        except Exception:
            try:
                _atomic_write_text(PROJECT_ROOT / ".env", original_env, 0o600)
            except OSError:
                logger.exception("failed to roll back the Telegram token environment file")
            raise
        if payload.get("clear_token"):
            os.environ.pop(key_name, None)
        elif token:
            os.environ[key_name] = token
        config = request.app.state.config
        push = config.setdefault("push", {})
        push["telegram"] = {
            **(push.get("telegram") or {}),
            "enabled": enabled,
            "apiKeyName": key_name,
            "chatId": chat_id,
        }
        return {"saved": True, "configured": bool(os.environ.get(key_name, "").strip() and chat_id)}

    @router.post("/api/console/telegram/test")
    async def test_console_telegram(request: Request, payload: dict = Body(default={}), _: None = Depends(guard)) -> dict:
        current = _telegram_config(request)
        key_name = str(payload.get("apiKeyName") or current.get("apiKeyName") or TELEGRAM_DEFAULT_KEY)
        token = str(payload.get("botToken") or "").strip() or os.environ.get(key_name, "").strip()
        chat_id = str(payload.get("chatId", current.get("chatId") or "")).strip()
        if not token or not chat_id:
            raise HTTPException(status_code=400, detail="complete the bot token and chat id first")

        from src.push.telegram import TelegramPlatform

        platform = TelegramPlatform({"apiKeyName": key_name, "chatId": chat_id, "request_timeout": 15})
        platform.bot_token = token
        try:
            await platform.send("News Agent 连通测试：Telegram 配置成功。", "News Agent")
            return {"ok": True, "message": "测试消息已发送，请查看 Telegram。"}
        except Exception as exc:
            logger.warning("telegram test push failed: %s", exc)
            return {"ok": False, "message": "发送失败，请检查 Bot Token 与 Chat ID。"}

    @router.post("/api/console/telegram/bind")
    async def bind_console_telegram(request: Request, _: None = Depends(guard)) -> dict:
        """Mint a one-time ``/start`` link, register the webhook, return a QR code.

        The user scans the QR in Telegram and presses START; Telegram then calls
        the webhook below with ``/start <token>`` and we learn the chat id —
        no manual copy/paste of a chat id.
        """
        current = _telegram_config(request)
        key_name = str(current.get("apiKeyName") or TELEGRAM_DEFAULT_KEY)
        token = os.environ.get(key_name, "").strip()
        if not token:
            raise HTTPException(status_code=400, detail="请先填写并保存 Bot Token")
        base = _public_base_url(request)
        if not base:
            raise HTTPException(status_code=400, detail="无法确定本实例的回调地址，请手动填写 Chat ID")

        me = await _telegram_api(token, "getMe")
        if not me.get("ok"):
            raise HTTPException(
                status_code=400,
                detail=f"Bot Token 无效：{me.get('description') or 'Telegram 拒绝'}",
            )
        username = str((me.get("result") or {}).get("username") or "")

        with session_scope() as session:
            secret = get_setting(session, "telegram_webhook_secret", "")
            if not secret:
                secret = secrets.token_urlsafe(24)
                set_setting(session, "telegram_webhook_secret", secret)
            bind_token = secrets.token_urlsafe(12)
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=TELEGRAM_BIND_TTL_MINUTES)
            set_setting(session, "telegram_bind_token", bind_token)
            set_setting(session, "telegram_bind_expires_at", expires_at.isoformat())

        webhook_url = f"{base}/api/telegram/webhook"
        hook = await _telegram_api(
            token,
            "setWebhook",
            {"url": webhook_url, "secret_token": secret, "allowed_updates": ["message"]},
        )
        link = f"https://t.me/{username}?start={bind_token}" if username else ""
        return {
            "link": link,
            "qr": _qr_data_uri(link),
            "bot": f"@{username}" if username else "",
            "expires_at": expires_at.isoformat(),
            "ttl_minutes": TELEGRAM_BIND_TTL_MINUTES,
            "webhook_url": webhook_url,
            "webhook_ok": bool(hook.get("ok")),
            "webhook_message": "已注册回调" if hook.get("ok") else str(hook.get("description") or "注册回调失败"),
        }

    @router.post("/api/telegram/webhook")
    async def telegram_webhook(request: Request) -> dict:
        """Telegram Bot API callback: bind the chat id sent with ``/start <token>``.

        Unauthenticated by design (Telegram calls it), but every request must
        carry the ``secret_token`` we registered with ``setWebhook``.
        """
        with session_scope() as session:
            secret = get_setting(session, "telegram_webhook_secret", "")
        header = request.headers.get("x-telegram-bot-api-secret-token", "")
        if not secret or not header or not secrets.compare_digest(secret, header):
            raise HTTPException(status_code=401, detail="unauthorized")

        try:
            update = await request.json()
        except Exception:
            return {"ok": True, "bound": False}

        message = (update or {}).get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id") or "")
        text = str(message.get("text") or "")
        if not chat_id or not text.startswith("/start"):
            return {"ok": True, "bound": False}

        parts = text.split(maxsplit=1)
        bind_token = parts[1].strip() if len(parts) > 1 else ""
        current = _telegram_config(request)
        key_name = str(current.get("apiKeyName") or TELEGRAM_DEFAULT_KEY)
        bot_token = os.environ.get(key_name, "").strip()

        with session_scope() as session:
            expected = get_setting(session, "telegram_bind_token", "")
            expires_raw = get_setting(session, "telegram_bind_expires_at", "")
        valid = bool(bind_token and expected and secrets.compare_digest(expected, bind_token))
        if valid and expires_raw:
            try:
                valid = datetime.fromisoformat(expires_raw) > datetime.now(timezone.utc)
            except ValueError:
                valid = False

        if not valid:
            if bot_token:
                await _send_telegram_message(bot_token, chat_id, "绑定链接已失效，请在管理后台重新生成二维码。")
            return {"ok": True, "bound": False}

        _store_telegram_chat_id(request, chat_id)
        with session_scope() as session:
            set_setting(session, "telegram_bind_token", "")
            set_setting(session, "telegram_bind_expires_at", "")
        if bot_token:
            await _send_telegram_message(bot_token, chat_id, "✅ 绑定成功，之后的投资信号会推送到这里。")
        return {"ok": True, "bound": True, "chat_id": chat_id}

    @router.post("/api/console/fetch-once", status_code=202)
    async def fetch_once(request: Request, _: None = Depends(guard)) -> dict:
        lock = request.app.state.fetch_lock
        if lock.locked():
            raise HTTPException(status_code=409, detail="fetch already running")
        await lock.acquire()
        request.app.state.fetch_state = {
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "report": None,
        }
        task = asyncio.create_task(_run_fetch_task(request.app))
        request.app.state.fetch_task = task
        return {"status": "running"}


    @router.get("/api/console/fetch-status")
    def fetch_status(request: Request, _: None = Depends(guard)) -> dict:
        return dict(request.app.state.fetch_state)

    return router
