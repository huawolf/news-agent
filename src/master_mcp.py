"""Master-side MCP server: how a slaver reports its local signals (§6).

The reporting channel is MCP, not a bespoke REST endpoint: both ends are
machines, MCP carries its own schema and discovery, and auth rides on the same
``Authorization`` header the pull endpoint uses.

Four wiring facts about ``mcp==1.29.0`` are baked in here rather than left to
the caller (§6.1):

* the child app is mounted at ``/api``, not ``/api/mcp`` — mounting at the full
  path makes Starlette 307-redirect ``POST /api/mcp`` to ``/api/mcp/``;
* the session manager is started by the parent FastAPI lifespan, because a
  mounted sub-app's own lifespan never runs;
* DNS rebinding protection is configured from config instead of left at the
  localhost-only default, which answers ``421`` behind Caddy;
* auth is a plain ASGI wrapper around the child app only, so the anonymous
  portal keeps working and no response body is buffered.

Identity reaches the tools through the ASGI scope: the wrapper verifies the JWT
and stores the resulting :class:`~src.auth.Identity` in ``scope``, the SDK
builds its ``Request`` from that same scope, and ``ctx.request_context.request``
exposes it. Nothing reads a user id from the request body.
"""
from __future__ import annotations

import hmac
import logging
import math
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.context import RequestContext
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_REQUEST, CallToolRequest, ErrorData
from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from src.auth import AuthError, Identity, extract_bearer, project, verify_token
from src.config import (
    VALID_LOCALES,
    get_mcp_config,
    get_output_language,
    get_report_config,
    get_translation_config,
)
from src.db.repositories import (
    ENERGY_REPORT_NEW,
    enabled_sector_specs,
    energy_balance,
    insert_signal,
    record_energy,
    recent_titles_for_dedup,
    reserve_scoring_slots,
    scoring_usage,
    replace_yield_pools as replace_yield_pool_rows,
)
from src.db.session import refresh_sqlite_fts, session_scope
from src.dedup.pipeline import apply as apply_dedup
from src.dedup.pipeline import apply_url_duplicate
from src.dedup.pipeline import prepare_candidate
from src.dedup.urls import is_valid_url
from src.llm import SCORED_FLAG, normalize_assets
from src.markdown_utils import normalize_str_list
from src.pipeline import classify_scored_entry, slaver_origin
from src.quotas import quota_payload, quota_window
from src.ranking import calculate_final_score
from src.translation import queue_missing_translations, store_reported_translation, translate_items

logger = logging.getLogger(__name__)

# Where the ASGI wrapper leaves the verified identity. Tools read it from the
# request scope, which is the same dict the wrapper wrote to.
IDENTITY_SCOPE_KEY = "news_agent.identity"
REPORTER_KEY_SCOPE_KEY = "news_agent.reporter_key"

SERVER_NAME = "news-agent-master"

# Ledger event names (§3.4). Kept as constants so the backoffice ledger query
# and this writer cannot drift apart.
EVENT_REPORT_NEW = "report_new"

# Field limits from the contract table (§6.3). Exceeding a limit is a per-item
# rejection, not a truncation: master must never silently shorten a payload the
# slaver believes it submitted.
MAX_URL_LEN = 2048
MAX_TITLE_LEN = 512
MAX_CONTENT_LEN = 8192
MAX_SUMMARY_LEN = 2048
MAX_SOURCE_NAME_LEN = 128
MAX_ASSETS = 5
MAX_KEYWORDS = 10
MAX_KEYWORD_LEN = 64

# Yield upload limits. The daily crawl emits tens of rows, so this is a guard
# against a malformed client, not a throughput knob.
MAX_YIELD_ITEMS = 500
MAX_POOL_NAME_LEN = 256
MAX_REFERENCE_LEN = 128
MAX_PROTOCOL_LEN = 128
MAX_CHAIN_LEN = 64

YIELD_REQUIRED_TEXT_FIELDS = (
    "pool_name",
    "url",
    "reference",
    "protocol",
    "chain",
)

# Accepted yield item keys. ``reporter`` and ``recorded_at`` are deliberately
# absent: both are server-side facts derived from the verified JWT and the
# write clock, so a client cannot forge either one.
YIELD_ITEM_FIELDS = (
    "pool_name",
    "url",
    "reference",
    "protocol",
    "chain",
    "tvl_usd",
    "apy_30d",
    "apy_today",
    "expiry_date",
)

DIRECTIONS = ("bullish", "bearish", "neutral")

# Accepted item keys. Anything else is dropped rather than rejected, so a newer
# slaver sending extra fields still works.
ITEM_FIELDS = (
    "url",
    "batch_id",
    "title",
    "content",
    "summary",
    "source_name",
    "published_at",
    "assets",
    "direction",
    "source_language",
    "source_keywords",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    """Best-effort ISO 8601 parse; ``published_at`` is optional."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- auth


def _matches_reporter_key(config: Dict, token: str) -> bool:
    """Timing-safe comparison against the shared yield-ingestion key."""
    expected = get_report_config(config)["reporter_key"]
    if not expected or not token:
        return False
    try:
        return hmac.compare_digest(str(token), expected)
    except TypeError:  # non-ASCII input: still not a valid key
        return False


class JWTAuthASGI:
    """Verify the manai-pic JWT before the MCP child app sees the request.

    Deliberately not FastAPI middleware: a global middleware would demand a JWT
    from the anonymous portal, and ``BaseHTTPMiddleware`` buffers the response
    body, which breaks the MCP SSE stream.

    A rejected token answers ``401`` with a plain JSON body instead of a
    JSON-RPC error. At this point no session exists yet, so an HTTP status is
    the accurate signal, and it lets the slaver tell "log in again" apart from
    "the batch was refused".
    """

    def __init__(self, app: Any, config: Dict):
        self.app = app
        self.config = config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":  # pragma: no cover - lifespan passes through
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers") or []
        }
        token = extract_bearer(headers)
        if _matches_reporter_key(self.config, token):
            # The shared reporter key is only good for the yield-upload tool.
            # No Identity is written, so report_signals/get_server_info still
            # fail closed at the tool boundary.
            scope[REPORTER_KEY_SCOPE_KEY] = True
            await self.app(scope, receive, send)
            return

        try:
            identity = await verify_token(self.config, token)
        except AuthError as exc:
            logger.warning("MCP auth rejected: %s", exc)
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        scope[IDENTITY_SCOPE_KEY] = identity
        await self.app(scope, receive, send)


# --------------------------------------------------------------------------- identity


def identity_from_context(ctx: Context) -> Identity:
    """Read the identity the ASGI wrapper verified for this very request.

    Raises when there is none: an unauthenticated call must fail loudly rather
    than fall back to a default user (§3.3).
    """
    request_context: RequestContext = ctx.request_context
    request = request_context.request
    scope: Dict[str, Any] = getattr(request, "scope", {}) or {}
    identity = scope.get(IDENTITY_SCOPE_KEY)
    if not isinstance(identity, Identity):
        raise ValueError("no verified identity on the request scope")
    return identity


# --------------------------------------------------------------------------- tools


def _reject(index: int, url: str, reason: str) -> Dict:
    return {"index": index, "url": url, "reason": reason}


def validate_item(index: int, raw: Any) -> Dict:
    """Validate one reported item.

    Returns ``{"ok": True, "entry": {...}}`` or ``{"ok": False, "reject": {...}}``.
    A single bad item must not fail the batch (§6.5), which is exactly why the
    tool takes ``list[dict]`` instead of a Pydantic model: SDK-level validation
    would reject all 200 items because of one.
    """
    if not isinstance(raw, dict):
        return {"ok": False, "reject": _reject(index, "", "invalid_field: item")}

    # Read-only whitelist (§6.3). ``score`` / ``sectors`` / ``keywords`` /
    # ``origin`` / ``reported_by`` are never read, so a slaver cannot inject a
    # self-assigned score or forge who reported a signal.
    item = {key: raw.get(key) for key in ITEM_FIELDS if key in raw}

    url = str(item.get("url") or "").strip()
    if len(url) > MAX_URL_LEN:
        return {"ok": False, "reject": _reject(index, url[:MAX_URL_LEN], "too_long: url")}
    if url and not is_valid_url(url):
        # Rejected here rather than downstream in ``prepare_candidate`` so the
        # reason is a stable contract string instead of a dedup side effect.
        return {"ok": False, "reject": _reject(index, url, "invalid_url")}

    batch_id = str(item.get("batch_id") or "").strip()
    if not batch_id or len(batch_id) > 64:
        return {"ok": False, "reject": _reject(index, url, "invalid_field: batch_id")}

    title = str(item.get("title") or "").strip()
    if not title:
        return {"ok": False, "reject": _reject(index, url, "missing_required_field: title")}
    if len(title) > MAX_TITLE_LEN:
        return {"ok": False, "reject": _reject(index, url, "too_long: title")}

    content = item.get("content")
    if not isinstance(content, str) or not content.strip():
        return {"ok": False, "reject": _reject(index, url, "missing_required_field: content")}
    if len(content) > MAX_CONTENT_LEN:
        return {"ok": False, "reject": _reject(index, url, "too_long: content")}

    summary = item.get("summary")
    if summary is not None and not isinstance(summary, str):
        return {"ok": False, "reject": _reject(index, url, "invalid_field: summary")}
    summary = (summary or "").strip()
    if len(summary) > MAX_SUMMARY_LEN:
        return {"ok": False, "reject": _reject(index, url, "too_long: summary")}

    source_name = str(item.get("source_name") or "").strip()
    if not source_name:
        return {
            "ok": False,
            "reject": _reject(index, url, "missing_required_field: source_name"),
        }
    if len(source_name) > MAX_SOURCE_NAME_LEN:
        return {"ok": False, "reject": _reject(index, url, "too_long: source_name")}

    published = None
    if item.get("published_at") is not None:
        published = _parse_iso(item.get("published_at"))
        if published is None:
            return {"ok": False, "reject": _reject(index, url, "invalid_field: published_at")}

    assets_raw = item.get("assets")
    if assets_raw is not None and not isinstance(assets_raw, list):
        return {"ok": False, "reject": _reject(index, url, "invalid_field: assets")}
    if isinstance(assets_raw, list) and len(assets_raw) > MAX_ASSETS:
        # The limit is contractual, so exceeding it is reported rather than
        # silently trimmed: the slaver must know which item was dropped.
        return {"ok": False, "reject": _reject(index, url, "too_long: assets")}

    direction = item.get("direction")
    if direction is not None and direction not in DIRECTIONS:
        direction = "neutral"

    # ``source_language`` is the locale the reported title/summary/keywords are
    # written in. It is optional: an older slaver that omits it is assumed to
    # already speak the master's main locale, which is the pre-translation
    # behaviour.
    source_language = str(item.get("source_language") or "").strip().lower()
    if source_language and source_language not in VALID_LOCALES:
        return {
            "ok": False,
            "reject": _reject(index, url, "invalid_field: source_language"),
        }

    # ``source_keywords`` are the slaver's own dedup keywords in its own
    # language. They never reach the main table — master rescoring produces the
    # main-locale keywords — but they are the source-locale translation's
    # keywords, so a report in another language keeps a real search surface
    # instead of an empty list.
    source_keywords = normalize_str_list(item.get("source_keywords"))[:MAX_KEYWORDS]
    source_keywords = [word[:MAX_KEYWORD_LEN] for word in source_keywords]

    return {
        "ok": True,
        "entry": {
            # ``link`` is the scorer's回填 key; the contract name is ``url``
            # and the mapping happens before the report leaves the slaver.
            "link": url or f"urn:news-agent:report:{index}:{uuid.uuid4().hex}",
            "url": url,
            "batch_id": batch_id,
            "title": title,
            "content": content,
            "summary": summary,
            "source": source_name,
            "published": published,
            "assets": normalize_assets(assets_raw or []),
            "direction": direction or "neutral",
            "source_language": source_language,
            "source_keywords": source_keywords,
        },
    }



def _reject_yield(index: int, pool_name: str, reason: str) -> Dict:
    return {"index": index, "pool_name": pool_name, "reason": reason}


def _finite_number(value: Any) -> Optional[float]:
    """Return a finite float, or ``None`` for strings/bools/NaN/infinity."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_expiry(value: Any) -> tuple[Optional[date], bool]:
    """Parse an ISO date. ``None`` and blank mean "no expiry"."""
    if value is None:
        return None, True
    if not isinstance(value, str):
        return None, False
    text = value.strip()
    if not text:
        return None, True
    try:
        return date.fromisoformat(text), True
    except ValueError:
        return None, False


def validate_yield_item(index: int, raw: Any) -> Dict:
    """Validate one yield-pool upload.

    Every field except the server-owned ``reporter`` / ``recorded_at`` must be
    present; ``expiry_date`` is the one nullable value, which is why it is
    checked for key presence rather than truthiness. Unknown keys are ignored
    so a newer crawler can add diagnostics without breaking an older master.
    """
    if not isinstance(raw, dict):
        return {"ok": False, "reject": _reject_yield(index, "", "invalid_field: item")}

    item = {key: raw.get(key) for key in YIELD_ITEM_FIELDS if key in raw}
    pool_name = str(item.get("pool_name") or "").strip()

    for field in YIELD_REQUIRED_TEXT_FIELDS:
        value = str(item.get(field) or "").strip()
        if not value:
            return {
                "ok": False,
                "reject": _reject_yield(
                    index, pool_name, f"missing_required_field: {field}"
                ),
            }

    if len(pool_name) > MAX_POOL_NAME_LEN:
        return {
            "ok": False,
            "reject": _reject_yield(index, pool_name, "too_long: pool_name"),
        }
    url = str(item.get("url") or "").strip()
    if len(url) > MAX_URL_LEN:
        return {
            "ok": False,
            "reject": _reject_yield(index, pool_name, "too_long: url"),
        }
    if not is_valid_url(url):
        return {
            "ok": False,
            "reject": _reject_yield(index, pool_name, "invalid_url"),
        }

    for field, limit in (
        ("reference", MAX_REFERENCE_LEN),
        ("protocol", MAX_PROTOCOL_LEN),
        ("chain", MAX_CHAIN_LEN),
    ):
        if len(str(item.get(field) or "").strip()) > limit:
            return {
                "ok": False,
                "reject": _reject_yield(index, pool_name, f"too_long: {field}"),
            }

    tvl_usd = _finite_number(item.get("tvl_usd"))
    if tvl_usd is None or tvl_usd <= 0:
        return {
            "ok": False,
            "reject": _reject_yield(index, pool_name, "invalid_number: tvl_usd"),
        }
    apy_30d = _finite_number(item.get("apy_30d"))
    if apy_30d is None:
        return {
            "ok": False,
            "reject": _reject_yield(index, pool_name, "invalid_number: apy_30d"),
        }
    apy_today = _finite_number(item.get("apy_today"))
    if apy_today is None:
        return {
            "ok": False,
            "reject": _reject_yield(index, pool_name, "invalid_number: apy_today"),
        }

    if "expiry_date" not in raw:
        return {
            "ok": False,
            "reject": _reject_yield(
                index, pool_name, "missing_required_field: expiry_date"
            ),
        }
    expiry_date, expiry_ok = _parse_expiry(raw.get("expiry_date"))
    if not expiry_ok:
        return {
            "ok": False,
            "reject": _reject_yield(index, pool_name, "invalid_date: expiry_date"),
        }

    return {
        "ok": True,
        "entry": {
            "pool_name": pool_name,
            "url": url,
            "reference": str(item["reference"]).strip(),
            "protocol": str(item["protocol"]).strip(),
            "chain": str(item["chain"]).strip(),
            "tvl_usd": tvl_usd,
            "apy_30d": apy_30d,
            "apy_today": apy_today,
            "expiry_date": expiry_date,
        },
    }


async def upsert_yield_pools(
    items: list[dict], reporter_key: str, ctx: Context, config: Dict
) -> dict[str, Any]:
    """Validate and atomically replace the full yield snapshot.

    ``reporter_key`` is a shared ingestion credential, not a user identity.
    The whole batch is validated before anything is deleted: one malformed
    item rejects the report and leaves the previous snapshot untouched. An
    empty list is a valid snapshot and clears the table.
    """
    del ctx  # the tool is authenticated by reporter_key, not a JWT identity
    expected = get_report_config(config)["reporter_key"]
    if not expected:
        raise ValueError("REPORTER_KEY is not configured on master")
    try:
        key_ok = hmac.compare_digest(str(reporter_key or ""), expected)
    except TypeError:
        key_ok = False
    if not key_ok:
        raise ValueError("invalid reporter_key")

    if not isinstance(items, list):
        raise ValueError("items must be a list")
    if len(items) > MAX_YIELD_ITEMS:
        raise ValueError(
            f"items holds {len(items)} entries, the limit is {MAX_YIELD_ITEMS}"
        )

    accepted: List[Dict] = []
    rejected: List[Dict] = []
    for index, raw in enumerate(items):
        checked = validate_yield_item(index, raw)
        if checked["ok"]:
            accepted.append(checked["entry"])
        else:
            rejected.append(checked["reject"])

    if rejected:
        logger.warning(
            "yield snapshot rejected key=%s received=%d rejected=%d; existing rows kept",
            "***",
            len(items),
            len(rejected),
        )
        return {
            "received": len(items),
            "deleted": 0,
            "inserted": 0,
            "replaced": False,
            "rejected": rejected,
        }

    with session_scope() as session:
        deleted, inserted = replace_yield_pool_rows(
            session,
            accepted,
            reporter=expected,
        )

    logger.info(
        "yield snapshot replaced reporter_key=%s received=%d deleted=%d inserted=%d",
        "***",
        len(items),
        deleted,
        inserted,
    )
    return {
        "received": len(items),
        "deleted": deleted,
        "inserted": inserted,
        "replaced": True,
        "rejected": [],
    }

async def score_reported(
    entries: List[Dict],
    config: Dict,
    session: Session,
) -> tuple[List[Dict], set[str]]:
    """Re-score on master and mark entries omitted by the model as duplicates.

    The scoring prompt removes duplicate items from the response instead of
    returning a marker. ``score_batch`` still returns one merged entry per input
    so correlation is preserved, but an entry without ``SCORED_FLAG`` means the
    model omitted it as a duplicate. The caller counts it as a duplicate rather
    than persisting or rejecting it.
    """
    from src.llm import score_batch

    if not entries:
        return [], set()

    sectors = enabled_sector_specs(session)
    if not sectors:
        raise RuntimeError("no enabled sector in the sector table; run the seed first")

    duplicate_titles = recent_titles_for_dedup(
        session, min_final_score=80.0, limit=100
    ) if entries else []
    scored, errors = await score_batch(
        entries, config.get("llm") or {}, sectors, duplicate_titles
    )
    for message in errors:
        logger.warning("report scoring error: %s", message)
    return scored, {item["id"] for item in duplicate_titles}


async def translate_reported(
    entries: List[Dict],
    config: Dict,
) -> Dict[int, Dict]:
    """Render reported text into the master's main locale.

    A slaver speaks one language; the main ``signal`` table speaks the master's
    ``output_language``. Without this step a Chinese report would land an
    English-language row whose title is Chinese — the exact mixed-language row
    the scoring prompt was changed to prevent.

    This runs before dedup on purpose: ``title_search`` is what BM25 compares,
    and a Chinese candidate would never match the English pool it shares.

    One call covers a batch because the translation prompt already takes an
    ``items`` array. Positions in the returned map line up with ``entries``;
    a position the model dropped is simply absent, and the caller rejects it.

    A failed chunk is logged and dropped instead of raised: one bad LLM
    response must not fail the whole report, and the affected items come back
    as ``untranslated: item`` like any other miss.

    The slaver's original text is left untouched on each entry — it becomes the
    source-locale translation, so translating it again would be wasted work.
    """
    main_locale = get_output_language(config)
    positions = [
        index
        for index, entry in enumerate(entries)
        if entry.get("source_language") not in ("", main_locale)
    ]
    if not positions:
        return {}

    batch_size = max(1, int(get_translation_config(config)["batch_size"]))
    translated: Dict[int, Dict] = {}
    for start in range(0, len(positions), batch_size):
        chunk = positions[start : start + batch_size]
        try:
            results = await translate_items(
                [
                    {
                        "link": entries[index].get("link", ""),
                        "title": entries[index].get("title", ""),
                        "summary": entries[index].get("summary", ""),
                    }
                    for index in chunk
                ],
                main_locale,
                config,
            )
        except Exception as error:  # noqa: BLE001 - per-chunk isolation
            logger.warning(
                "report translation failed size=%d locale=%s error=%s",
                len(chunk),
                main_locale,
                error,
            )
            continue
        for index in chunk:
            result = results.get(entries[index].get("link") or "")
            if result is not None:
                translated[index] = result
    return translated


def _signal_values(
    prepared: Dict,
    *,
    origin: str,
    reported_by: str,
    now: datetime,
    batch_id: str,
    config: Dict | None = None,
) -> Dict:
    published = prepared.get("published")
    score = float(prepared.get("score") or 0.0)
    assets = list(prepared.get("assets") or [])
    return {
        "url": prepared["canonical_url"] or None,
        "url_hash": prepared["url_hash"],
        "source_urls": [prepared["canonical_url"]] if prepared["canonical_url"] else [],
        "title": prepared.get("title") or "",
        "summary": prepared.get("summary") or "",
        "content": prepared.get("content") or "",
        "title_search": prepared["title_search"],
        "summary_search": prepared.get("summary_search", ""),
        "keywords_search": prepared.get("keywords_search", ""),
        "content_search": prepared["content_search"],
        "keywords": list(prepared.get("keywords") or []),
        "source_name": prepared.get("source") or "",
        "published_at": published if isinstance(published, datetime) else None,
        "first_seen_at": now,
        "batch_id": batch_id,
        "score": score,
        "final_score": calculate_final_score(
            score,
            assets=assets,
            first_seen_at=now,
            source_name=prepared.get("source") or "",
            text=f"{prepared.get('title') or ''} {prepared.get('content') or ''}",
            config=config,
        ),
        "sectors": list(prepared.get("sectors") or []),
        "assets": assets,
        "direction": prepared.get("direction") or "neutral",
        "region": prepared.get("region"),
        "origin": origin,
        "reported_by": reported_by,
    }


async def store_reported(
    session: Session,
    config: Dict,
    scored: Sequence[Dict],
    identity: Identity,
    *,
    rejected: List[Dict],
) -> Dict:
    """Persist scored items, crediting energy per displayable signal.

    Invariants held here (§6.3): ``received == inserted + hidden + duplicates +
    len(rejected)`` and ``energy_credited == inserted``.  Hidden rows are stored
    so the reporter's high-score denominator is complete, but they never get a
    translation, energy credit or place in the portal.

    Each item runs inside a savepoint. A single failure must not roll back the
    items already written — and because the whole batch shares one outer
    transaction, the savepoint is what keeps that guarantee true.

    A reported signal lands in the main table in the master's main locale. If
    the slaver spoke another locale, its own text becomes that locale's
    completed translation row; otherwise every configured target locale is
    queued for the translation worker.
    """
    now = datetime.now(timezone.utc)
    origin = slaver_origin(identity.owner)
    user_id = identity.user_id

    inserted = 0
    hidden = 0
    duplicates = 0

    for index, entry in scored:
        # Scored entries address the article by ``link``; the rest of the dedup
        # and storage path speaks ``url`` (the contract name), so translate once
        # here rather than making every downstream helper aware of both.
        prepared = prepare_candidate(
            {**entry, "url": entry.get("url") if "url" in entry else entry.get("link")}
        )
        if prepared is None:
            rejected.append(_reject(index, str(entry.get("link") or ""), "invalid_url"))
            continue

        is_duplicate, _decision = await apply_dedup(session, config, prepared)
        if is_duplicate:
            duplicates += 1
            continue

        nested = session.begin_nested()
        try:
            with nested:
                row = insert_signal(
                    session,
                    _signal_values(
                        prepared,
                        origin=origin,
                        reported_by=user_id,
                        now=now,
                        batch_id=str(entry["batch_id"]),
                        config=config,
                    ),
                )
                session.flush()
                refresh_sqlite_fts(session, [row.url_hash])
                if entry.get("_hidden"):
                    hidden += 1
                else:
                    source = entry.get("source_translation")
                    if isinstance(source, dict) and source.get("locale"):
                        store_reported_translation(
                            session,
                            row.id,
                            str(source["locale"]),
                            {
                                "title": source.get("title") or "",
                                "summary": source.get("summary") or "",
                                # The slaver's own keywords: master rescoring
                                # produced the main-locale ones and those must not
                                # leak into the source-locale row.
                                "keywords": source.get("keywords") or [],
                            },
                        )
                    else:
                        queue_missing_translations(session, [row.id], config)
                    record_energy(
                        session, user_id, EVENT_REPORT_NEW, ENERGY_REPORT_NEW, row.id
                    )
                    inserted += 1
        except Exception as exc:  # noqa: BLE001 - one bad item must not fail the batch
            if nested.is_active:
                nested.rollback()
            logger.warning("report item %s failed: %s", index, exc)
            rejected.append(
                _reject(index, prepared["canonical_url"], f"invalid_field: item ({exc})")
            )

    return {
        "inserted": inserted,
        "hidden": hidden,
        "duplicates": duplicates,
        "energy_credited": inserted,
    }


async def report_signals(items: list, ctx: Context, config: Dict) -> Dict[str, Any]:
    """Report locally collected signals to master (§6.3).

    The signature keeps ``items`` a flat list of dicts on purpose: a Pydantic
    model parameter would make the SDK wrap the payload (``{"payload": {...}}``)
    and one invalid item would fail the entire array.
    """
    identity = identity_from_context(ctx)
    received = len(items or [])

    rejected: List[Dict] = []
    entries: List[Dict] = []
    positions: List[int] = []

    for index, raw in enumerate(items or []):
        checked = validate_item(index, raw)
        if not checked["ok"]:
            rejected.append(checked["reject"])
            continue
        entries.append(checked["entry"])
        positions.append(index)

    report_config = get_report_config(config)
    window = quota_window(report_config["quota_timezone"])
    with session_scope() as session:
        project(session, identity)

        # Cheap URL dedup comes first: a retry of a batch that already landed
        # must not consume the reporter's daily scoring budget.  BM25 waits
        # until after translation so it compares same-language text.
        url_entries: List[Dict] = []
        url_positions: List[int] = []
        url_duplicates = 0
        for position, entry in zip(positions, entries, strict=True):
            prepared = prepare_candidate({
                **entry,
                "url": entry.get("url", entry.get("link")),
            })
            if prepared is None:
                rejected.append(
                    _reject(position, str(entry.get("link") or ""), "invalid_url")
                )
                continue
            if apply_url_duplicate(session, prepared):
                url_duplicates += 1
                continue
            url_entries.append(entry)
            url_positions.append(position)

        reserved = reserve_scoring_slots(
            session,
            identity.user_id,
            window.usage_date,
            report_config["daily_limit_per_user"],
            len(url_entries),
        )
        if reserved < len(url_entries):
            for position, entry in zip(
                url_positions[reserved:], url_entries[reserved:], strict=True
            ):
                rejected.append(
                    _reject(
                        position,
                        str(entry.get("link") or ""),
                        "quota_exceeded: daily scoring limit",
                    )
                )
            url_entries = url_entries[:reserved]
            url_positions = url_positions[:reserved]
        entries = url_entries
        positions = url_positions

        # A slaver speaks one language, the main ``signal`` table speaks the
        # master's. Render the reported text into the main locale up front so
        # dedup compares like with like, and keep the slaver's own wording
        # aside — it is the source-locale translation.
        main_locale = get_output_language(config)
        translated = await translate_reported(entries, config)
        renderable: List[Dict] = []
        renderable_positions: List[int] = []
        for index, entry in enumerate(entries):
            source_language = str(entry.get("source_language") or "")
            if source_language and source_language != main_locale:
                result = translated.get(index)
                if result is None:
                    rejected.append(
                        _reject(
                            positions[index],
                            str(entry.get("link") or ""),
                            "untranslated: item",
                        )
                    )
                    continue
                entry["source_translation"] = {
                    "locale": source_language,
                    "title": entry.get("title", ""),
                    "summary": entry.get("summary", ""),
                    "keywords": entry.get("source_keywords") or [],
                }
                entry["title"] = result["title"]
                entry["summary"] = result["summary"]
            renderable.append(entry)
            renderable_positions.append(positions[index])
        entries = renderable
        positions = renderable_positions

        unique_entries: List[Dict] = []
        unique_positions: List[int] = []
        duplicates = url_duplicates
        for position, entry in zip(positions, entries, strict=True):
            score_key = entry.get("link") or f"urn:news-agent:signal:{uuid.uuid4().hex}"
            prepared = prepare_candidate({
                **entry,
                "url": entry.get("url", entry.get("link")),
                "link": score_key,
            })
            if prepared is None:
                rejected.append(_reject(position, str(entry.get("link") or ""), "invalid_url"))
                continue
            nested = session.begin_nested()
            with nested:
                is_duplicate, _decision = await apply_dedup(session, config, prepared)
            if is_duplicate:
                duplicates += 1
                continue
            unique_entries.append({**entry, "link": score_key})
            unique_positions.append(position)

        scored, historical_ids = await score_reported(unique_entries, config, session)

        paired: List[tuple] = []
        for position, entry in zip(unique_positions, scored, strict=True):
            if SCORED_FLAG in entry and not entry.get(SCORED_FLAG):
                # The scoring prompt removes duplicate items from the response.
                # Missing output is a duplicate, not a rejected report item.
                duplicates += 1
                continue
            if entry.get("is_duplicate") and (
                not entry.get("duplicate_of")
                or str(entry.get("duplicate_of") or "") in historical_ids
            ):
                duplicates += 1
                continue
            hidden, stored_score = classify_scored_entry(entry)
            entry["score"] = stored_score
            entry["_hidden"] = hidden
            paired.append((position, entry))

        counters = await store_reported(
            session, config, paired, identity, rejected=rejected
        )
        counters["duplicates"] += duplicates
        quota = _quota_payload(session, identity.user_id, config, window)

    logger.info(
        "report_signals user=%s received=%d inserted=%d hidden=%d duplicates=%d rejected=%d",
        identity.user_id,
        received,
        counters["inserted"],
        counters["hidden"],
        counters["duplicates"],
        len(rejected),
    )

    return {
        "received": received,
        "scored": counters["inserted"] + counters["hidden"],
        "inserted": counters["inserted"],
        "hidden": counters["hidden"],
        "duplicates": counters["duplicates"],
        "rejected": rejected,
        "energy_credited": counters["energy_credited"],
        "quota": quota,
        "server_time": _utc_now_iso(),
    }


def _quota_payload(
    session: Session,
    user_id: str,
    config: Dict,
    window=None,
) -> Dict[str, Any]:
    report_config = get_report_config(config)
    current = window or quota_window(report_config["quota_timezone"])
    used = scoring_usage(session, user_id, current.usage_date)
    return quota_payload(report_config["daily_limit_per_user"], used, current)


def server_info(session: Session, identity: Identity, config: Dict) -> Dict[str, Any]:
    return {
        "user_id": identity.user_id,
        "server_time": _utc_now_iso(),
        # Only a local hint: the report payload never carries sectors (§6.4).
        "sectors": [spec["name"] for spec in enabled_sector_specs(session)],
        "energy_balance": energy_balance(session, identity.user_id),
        "quota": _quota_payload(session, identity.user_id, config),
    }


# --------------------------------------------------------------------------- wiring


def _transport_security(mcp_config: Dict) -> Optional[TransportSecuritySettings]:
    """DNS rebinding protection, configured rather than defaulted.

    The SDK turns it on for localhost and only allows localhost. Behind Caddy
    the Host is the public domain, which that default rejects with ``421``.
    Empty allow-lists in config mean "off", which is the right choice when the
    service is only reachable through the gateway.
    """
    hosts = mcp_config["allowed_hosts"]
    origins = mcp_config["allowed_origins"]
    if not hosts and not origins:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def _install_batch_guard(mcp: FastMCP, max_batch: int) -> None:
    """Turn an oversized batch into a JSON-RPC ``InvalidRequest``.

    §6.5 forbids silent truncation: a slaver that believes all 200 landed would
    stop resending them. But an exception raised inside a tool becomes
    ``CallToolResult(isError=True)``, which the slaver cannot classify as a
    protocol error. Only an :class:`McpError` escaping the handler reaches
    ``_handle_request`` and turns into ``ErrorData``, so the check lives in a
    wrapper around the registered handler.

    This depends on the low-level server's ``request_handlers`` dict, which is
    public but not part of FastMCP's own API; the protocol test pins the
    behaviour so an SDK upgrade that changes it fails loudly.
    """
    handlers = mcp._mcp_server.request_handlers
    inner = handlers.get(CallToolRequest)
    if inner is None:  # pragma: no cover - SDK always registers this
        raise RuntimeError("CallToolRequest handler missing; cannot enforce batch limit")

    async def guarded(req):
        if req.params.name == "report_signals":
            items = (req.params.arguments or {}).get("items")
            if isinstance(items, list) and len(items) > max_batch:
                raise McpError(
                    ErrorData(
                        code=INVALID_REQUEST,
                        message=(
                            f"items holds {len(items)} entries, the limit is {max_batch}; "
                            "split the batch instead of relying on truncation"
                        ),
                    )
                )
        return await inner(req)

    handlers[CallToolRequest] = guarded


def create_master_mcp(config: Dict) -> FastMCP:
    """Build the FastMCP server with both tools bound to this config."""
    mcp_config = get_mcp_config(config)
    report_config = get_report_config(config)

    mcp = FastMCP(
        name=mcp_config["server_name"] or SERVER_NAME,
        streamable_http_path=mcp_config["streamable_http_path"],
        max_request_body_size=mcp_config["max_request_body_size"],
        transport_security=_transport_security(mcp_config),
    )

    # The public tool names must stay ``report_signals`` / ``get_server_info``
    # while the config stays bound in a closure: the SDK builds the input schema
    # from the signature, so a ``config`` parameter would leak into it.
    #
    # Builtin ``dict[str, Any]``, NOT ``typing.Dict[str, Any]``: the SDK checks
    # ``isinstance(annotation, types.GenericAlias)`` and typing's ``Dict`` is a
    # ``typing._GenericAlias``, so it misses the str-keyed-dict branch and wraps
    # the result as ``{"result": {...}}``. The slaver would have to unwrap.
    async def report_signals_tool(items: list[dict], ctx: Context) -> dict[str, Any]:
        return await report_signals(items, ctx, config)

    async def upsert_yield_pools_tool(
        items: list[dict], reporter_key: str, ctx: Context
    ) -> dict[str, Any]:
        return await upsert_yield_pools(items, reporter_key, ctx, config)

    async def get_server_info_tool(ctx: Context) -> dict[str, Any]:
        identity = identity_from_context(ctx)
        # No ``project()`` here: §6.4 defines this as a pure self-check, and
        # upserting the user_profile projection would make every connectivity
        # probe a write.
        with session_scope() as session:
            return server_info(session, identity, config)

    mcp.add_tool(
        report_signals_tool,
        name="report_signals",
        description=REPORT_SIGNALS_DESCRIPTION,
    )
    mcp.add_tool(
        upsert_yield_pools_tool,
        name="upsert_yield_pools",
        description=UPSERT_YIELD_POOLS_DESCRIPTION,
    )
    mcp.add_tool(
        get_server_info_tool,
        name="get_server_info",
        description=SERVER_INFO_DESCRIPTION,
    )

    _install_batch_guard(mcp, report_config["batch_size"])
    return mcp


REPORT_SIGNALS_DESCRIPTION = (
    "Report signals collected locally by a slaver. Takes a flat 'items' array; "
    "each item needs title, content and source_name; url is optional and "
    "URL-less items are deduplicated with BM25 only. Items may carry summary, "
    "published_at, assets, direction, source_language and source_keywords. "
    "source_language is the locale the reported title/summary/keywords are "
    "written in; master renders them into its own main locale and keeps the "
    "slaver's wording as that locale's translation. Scoring and sector "
    "assignment are recomputed on master; unknown fields are ignored. Returns "
    "per-batch counters plus a 'rejected' list carrying the input index and a "
    "stable reason prefix."
)

UPSERT_YIELD_POOLS_DESCRIPTION = (
    "Replace the entire yield-pool snapshot with a crawl result. Requires the "
    "shared reporter_key configured in master's REPORTER_KEY; no manai-pic JWT "
    "is used on this path. Takes a flat 'items' array; each item needs "
    "pool_name, url, reference, protocol, chain, tvl_usd, apy_30d, apy_today "
    "and expiry_date. expiry_date may be null but the key must be present. "
    "The whole batch is validated first; any rejected item leaves the existing "
    "snapshot untouched. On success old rows are deleted and the new snapshot "
    "is inserted in one transaction. An empty items array clears the table. "
    "Returns received/deleted/inserted/replaced counters and a 'rejected' list."
)

SERVER_INFO_DESCRIPTION = (
    "Connectivity and identity self-check: returns the authenticated user id, "
    "server time, the sectors currently enabled on master, and the caller's "
    "energy balance. Performs no writes."
)


@asynccontextmanager
async def run_session_manager(mcp: FastMCP):
    """Run the MCP session manager from the parent app's lifespan.

    A mounted sub-app's own lifespan never fires, so without this the handshake
    dies with an ``ExceptionGroup``.
    """
    async with mcp.session_manager.run():
        yield


def mount_master_mcp(app, config: Dict) -> FastMCP:
    """Mount the protected MCP child app and return the server instance.

    The caller is responsible for running :func:`run_session_manager` in the
    parent lifespan.
    """
    mcp = create_master_mcp(config)
    child: Starlette = mcp.streamable_http_app()
    app.mount(get_mcp_config(config)["mount_path"], JWTAuthASGI(child, config))
    return mcp


__all__ = [
    "EVENT_REPORT_NEW",
    "IDENTITY_SCOPE_KEY",
    "JWTAuthASGI",
    "REPORTER_KEY_SCOPE_KEY",
    "create_master_mcp",
    "identity_from_context",
    "mount_master_mcp",
    "report_signals",
    "run_session_manager",
    "upsert_yield_pools",
    "validate_yield_item",
    "server_info",
    "validate_item",
]
