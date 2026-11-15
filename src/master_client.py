"""Slaver-side MCP client for the master report channel (§6).

One long-lived Streamable HTTP session is held across fetch cycles: paying the
``initialize`` handshake per batch would multiply the fixed cost by the batch
count (§6.1). The session is rebuilt only when the connection is lost or the
token changes.

Failure classes are kept apart because the slaver must react differently
(§6.5): a ``401`` means re-login and no retry, anything transport-related means
the whole batch failed and a retry of the same batch is safe to attempt (URL or
BM25 dedup may catch already-stored signals), while per-item ``rejected``
entries are permanent and must not be retried.
"""
from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from src.config import get_output_language, get_report_config, get_server_url

logger = logging.getLogger(__name__)

# Contract field mapping (§6.3). The scoring chain speaks ``link`` / ``source``
# and the MCP contract speaks ``url`` / ``source_name`` — same values, one
# rename at the report boundary, no third alias. URL is optional and blank for
# URL-less signals. Only these fields are ever
# built; ``score`` / ``sectors`` / ``origin`` / ``reported_by`` are master-side
# values (§6.3) and never leave the slaver. The local ``keywords`` travel as
# ``source_keywords`` because master keeps them as the source-locale
# translation's keywords; its own keywords are produced by master rescoring.

MAX_TITLE_LEN = 512
MAX_SUMMARY_LEN = 2048
MAX_SOURCE_NAME_LEN = 128
MAX_ASSETS = 5
MAX_KEYWORDS = 10
DIRECTIONS = ("bullish", "bearish", "neutral")


class MasterAuthRequired(Exception):
    """Master answered ``401``: the token expired, re-login, do not retry."""


class MasterReportError(Exception):
    """Transport/protocol failure: the whole batch failed, retry is safe."""


def _find_http_status(exc: BaseException) -> Optional[httpx.HTTPStatusError]:
    """Dig an ``HTTPStatusError`` out of an exception (group).

    The SDK surfaces failures inside AnyIO ``ExceptionGroup``s, so a plain
    ``isinstance`` check on the top-level exception misses a ``401`` and the
    client would retry a batch that can never succeed until re-login.
    Iterative with a seen set because ``__context__`` chains can cycle.
    """
    pending = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, httpx.HTTPStatusError):
            return current
        if isinstance(current, BaseExceptionGroup):  # noqa: UP040
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return None


def truncate_utf8(value: str, max_bytes: int) -> str:
    """Cut to ``max_bytes`` of UTF-8 without splitting a character.

    Byte-based because the master's 4 MiB request-body limit counts bytes
    (§6.1); a char-based cut would let Chinese content blow the limit while
    passing a ``len()`` check. Bytes bound chars from above, so the result also
    satisfies master's per-item length validation.
    """
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()


def _iso(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return str(value)


def to_report_item(
    raw: Any, content_max_bytes: int, source_language: str = ""
) -> Dict[str, Any]:
    """Map one local signal onto the contract shape (§6.3 字段名映射).

    Accepts an ORM row or a plain dict. Only whitelist fields are emitted;
    anything the caller accidentally attached (``score``, ``origin``, …) is
    dropped here so the master never sees it even though its own whitelist
    would also ignore it.

    ``source_language`` is this instance's ``output_language``: master needs it
    to know whether the reported title/summary already speak its main locale.
    ``source_keywords`` travel with it because master stores them as the
    source-locale translation's keywords rather than re-deriving them.
    """
    def get(*names: str) -> Any:
        for name in names:
            if isinstance(raw, dict):
                if name in raw:
                    return raw[name]
            else:
                value = getattr(raw, name, None)
                if value is not None:
                    return value
        return None

    url = get("url", "link") or ""
    content = get("content") or ""
    source_name = get("source_name", "source") or ""

    batch_id = get("batch_id")
    item: Dict[str, Any] = {
        "url": str(url),
        "batch_id": str(batch_id or ""),
        "title": str(get("title") or ""),
        "content": truncate_utf8(str(content), content_max_bytes),
        "source_name": str(source_name),
    }

    summary = get("summary")
    if summary:
        item["summary"] = truncate_utf8(str(summary), MAX_SUMMARY_LEN)

    published_at = _iso(get("published_at", "published"))
    if published_at:
        item["published_at"] = published_at

    assets = get("assets")
    if isinstance(assets, (list, tuple)):
        normalized = [
            str(a).strip().upper().replace("$", "").replace(" ", "")
            for a in assets
            if str(a).strip()
        ]
        item["assets"] = normalized[:MAX_ASSETS]

    direction = get("direction")
    if direction in DIRECTIONS:
        item["direction"] = direction

    locale = str(source_language or "").strip().lower()
    if locale:
        item["source_language"] = locale

    keywords = get("keywords")
    if isinstance(keywords, (list, tuple)):
        normalized_keywords = [
            str(word).strip()[:64] for word in keywords if str(word).strip()
        ]
        if normalized_keywords:
            item["source_keywords"] = normalized_keywords[:MAX_KEYWORDS]

    return item


def split_batches(
    items: Sequence[Dict[str, Any]], batch_size: int
) -> Iterable[List[Dict[str, Any]]]:
    for start in range(0, len(items), batch_size):
        yield list(items[start : start + batch_size])


class MasterMCPClient:
    """Long-lived MCP session against ``{server_url}/api/mcp`` (§6.1)."""

    def __init__(
        self,
        config: Dict,
        token: str = "",
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.config = config
        report = get_report_config(config)
        self.batch_size = report["batch_size"]
        self.content_max_bytes = report["content_max_bytes"]
        # Reported verbatim so master knows whether the text it receives is
        # already written in its main locale.
        self.source_language = get_output_language(config)
        server_url = get_server_url(config)
        self.endpoint = f"{server_url}{report['path']}" if server_url else ""
        self._token = token
        self._transport = transport
        self._stack: Optional[AsyncExitStack] = None
        self._session: Optional[ClientSession] = None
        self._http: Optional[httpx.AsyncClient] = None

    # ------------------------------------------------------------- lifecycle

    @property
    def connected(self) -> bool:
        return self._session is not None

    async def connect(self) -> ClientSession:
        """Open the session (or return the live one).

        Rebuilds from scratch after a failure: the previous context exits, a
        fresh ``initialize`` runs, and the server issues a new session id.
        """
        if self._session is not None:
            return self._session
        if not self.endpoint:
            raise MasterReportError("server_url is empty; not a slaver instance")
        if not self._token:
            raise MasterAuthRequired("no manai-pic token; log in first")

        stack = AsyncExitStack()
        self._stack = stack
        self._http = httpx.AsyncClient(
            transport=self._transport,
            headers={"Authorization": f"Bearer {self._token}"},
        )
        try:
            streams = await stack.enter_async_context(
                streamable_http_client(self.endpoint, http_client=self._http)
            )
            session = await stack.enter_async_context(
                ClientSession(streams[0], streams[1])
            )
            await session.initialize()
        except Exception as exc:
            # ``close()`` owns teardown: a failed handshake can leave the SDK's
            # AnyIO cancel scopes out of order, and exiting them from this
            # handler raised ``RuntimeError`` that masked the real error.
            await self.close()
            self._raise_classified(exc)

        self._session = session
        logger.info("MCP session established with %s", self.endpoint)
        return session

    async def close(self) -> None:
        stack, self._stack = self._stack, None
        session, self._session = self._session, None
        http_client, self._http = self._http, None
        del session  # lifetime is owned by the stack
        if stack is not None:
            try:
                await stack.aclose()
            except Exception as exc:  # noqa: BLE001 - teardown must not mask
                logger.warning("MCP session teardown: %s", exc)
        if http_client is not None:
            await http_client.aclose()

    async def __aenter__(self) -> "MasterMCPClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def update_token(self, token: str) -> None:
        """Apply a fresh token; drops the session so the next call re-auths."""
        token = (token or "").strip()
        if token == self._token:
            return
        self._token = token
        await self.close()

    async def _reset(self) -> None:
        await self.close()

    def _raise_classified(self, exc: BaseException) -> None:
        status = _find_http_status(exc)
        if status is not None and status.response.status_code in (401, 403):
            raise MasterAuthRequired(
                f"master rejected the token ({status.response.status_code})"
            ) from exc
        if isinstance(exc, (MasterAuthRequired, MasterReportError)):
            raise exc
        raise MasterReportError(str(exc)) from exc

    async def _call(self, tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        try:
            session = await self.connect()
            result = await session.call_tool(tool, arguments)
        except Exception as exc:
            # Drop the session so the next attempt performs a fresh handshake
            # instead of writing into a dead stream.
            await self._reset()
            self._raise_classified(exc)
            raise  # unreachable; _raise_classified always raises

        if result.isError:
            # A tool-level error (e.g. oversized batch on master) leaves the
            # session usable; it is the master's answer, not a broken link.
            text = next(
                (c.text for c in result.content if hasattr(c, "text")), tool
            )
            raise MasterReportError(f"{tool} failed: {text}")

        structured = result.structuredContent or {}
        return dict(structured)

    # ------------------------------------------------------------- tools

    async def get_server_info(self) -> Dict[str, Any]:
        """Connectivity/identity self-check (§6.4), no writes."""
        return await self._call("get_server_info", {})

    async def report(self, signals: Sequence[Any]) -> Dict[str, Any]:
        """Report local signals in contract batches (§5.6 step 7, §6.5).

        Returns aggregated counters with ``rejected[].index`` remapped to the
        original input positions, so the caller can mark exactly which local
        rows failed regardless of which batch they landed in.
        """
        if not signals:
            return _empty_report()

        items = [
            to_report_item(signal, self.content_max_bytes, self.source_language)
            for signal in signals
        ]

        aggregate = _empty_report()
        aggregate["received"] = len(items)

        offset = 0
        for batch in split_batches(items, self.batch_size):
            result = await self._call("report_signals", {"items": batch})
            aggregate["inserted"] += int(result.get("inserted", 0))
            aggregate["hidden"] += int(result.get("hidden", 0))
            aggregate["duplicates"] += int(result.get("duplicates", 0))
            aggregate["energy_credited"] += int(result.get("energy_credited", 0))
            if isinstance(result.get("quota"), dict):
                aggregate["quota"] = result["quota"]
            if result.get("server_time"):
                aggregate["server_time"] = result["server_time"]
            for rejected in result.get("rejected") or []:
                rejected = dict(rejected)
                rejected["index"] = int(rejected.get("index", 0)) + offset
                aggregate["rejected"].append(rejected)
            offset += len(batch)

        if aggregate["received"] != (
            aggregate["inserted"]
            + aggregate["hidden"]
            + aggregate["duplicates"]
            + len(aggregate["rejected"])
        ):
            # Would break the §6.3 invariant; surface loudly rather than let
            # the caller assume unaccounted rows landed.
            logger.warning(
                "report counters do not add up: %s", aggregate
            )
        return aggregate


def _empty_report() -> Dict[str, Any]:
    return {
        "received": 0,
        "inserted": 0,
        "hidden": 0,
        "duplicates": 0,
        "rejected": [],
        "energy_credited": 0,
        "quota": None,
        "server_time": None,
    }


__all__ = [
    "MasterAuthRequired",
    "MasterMCPClient",
    "MasterReportError",
    "split_batches",
    "to_report_item",
    "truncate_utf8",
]
