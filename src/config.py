"""Configuration loading for the master/slaver signal system.

Two modes only: ``master`` and ``slaver``. Whether a slaver talks to a master is
decided by ``server_url`` being empty or not, not by a third mode value.
"""
import json
import os
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import quote, urlsplit, urlunsplit

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESOURCES_DIR = PROJECT_ROOT / "resources"
load_dotenv(PROJECT_ROOT / ".env", override=False)

VALID_MODES = ("master", "slaver")

#: Upper bound for the user-written natural-language ranking preference.
MAX_PREFERENCE_CHARS = 4000

# Only two content locales exist; `output_language` picks which one the main
# `signal` table speaks, and the rest land in `signal_translation`.
VALID_LOCALES = ("zh", "en")

#: Signals scoring below this never enter the pool (§5.1).
DEFAULT_MIN_STORE_SCORE = 60

#: The digest only carries signals that clear this floor (§5.3).
DEFAULT_PUSH_MIN_SCORE = 80

#: Each reporter may send at most this many candidates into LLM scoring per
#: local calendar day. The cap counts requests, not stored rows.
DEFAULT_DAILY_SCORING_LIMIT = 100
DEFAULT_QUOTA_TIMEZONE = "UTC"

DEFAULT_DATABASE_URL = ""
DEFAULT_SCHEMA = "news_agent"


def get_mode(config: Dict) -> str:
    mode = str(config.get("mode") or "slaver").strip().lower()
    if mode not in VALID_MODES:
        raise ValueError(
            f"invalid mode {mode!r}; expected one of {VALID_MODES}"
        )
    return mode


def is_master(config: Dict) -> bool:
    return get_mode(config) == "master"


def get_server_url(config: Dict) -> str:
    """Empty when the instance runs purely locally (slaver without master)."""
    return str(config.get("server_url") or "").strip().rstrip("/")


def uses_master(config: Dict) -> bool:
    """True when this instance pulls the shared pool and reports local signals."""
    return not is_master(config) and bool(get_server_url(config))


def get_database_url(config: Dict) -> str:
    """Resolve the SQLAlchemy URL, defaulting slaver to a local SQLite file.

    ``NEWS_AGENT_DATABASE_URL`` wins over the file so migrations and the app
    always agree on which database they are talking to.
    """
    override = os.environ.get("NEWS_AGENT_DATABASE_URL", "").strip()
    if override:
        return override

    configured = str(
        config.get("database", {}).get("url") or DEFAULT_DATABASE_URL
    ).strip()
    if configured:
        return configured

    if is_master(config):
        raise ValueError(
            "database.url is required in master mode (PostgreSQL DSN)"
        )

    sqlite_path = str(
        config.get("database", {}).get("sqlite_path") or "data/news_agent.db"
    ).strip()
    path = Path(sqlite_path)
    if not path.is_absolute():
        data_root = os.environ.get("NEWS_AGENT_DATA_DIR", "").strip()
        path = Path(data_root).expanduser() / path.name if data_root else PROJECT_ROOT / path
    return f"sqlite:///{path}"


def redact_database_url(url: str) -> str:
    """Return a log-safe database URL with the password replaced.

    SQLAlchemy DSNs commonly embed credentials. They must never reach CLI
    output, startup logs, or bug reports verbatim.
    """
    if not url:
        return ""
    parsed = urlsplit(str(url))
    if parsed.password is None:
        return str(url)

    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    user = quote(parsed.username or "", safe="")
    netloc = f"{user}:***@{host}" if user else f"***@{host}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def get_schema(config: Optional[Dict] = None) -> str:
    if config is None:
        config = _try_load_config()
    return str(
        (config.get("database", {}) or {}).get("schema") or DEFAULT_SCHEMA
    ).strip()


def _try_load_config() -> Dict:
    cfg_path = os.environ.get("NEWS_AGENT_CONFIG", "config.json")
    try:
        return load_config(cfg_path)
    except Exception:
        return {}


#: The BM25 query used to be the candidate's own title plus body, i.e.
#: potentially hundreds of tokens with HTML words, punctuation tokens and
#: repeats. Summing that many contributions inflated every score by orders of
#: magnitude (measured: unrelated pairs reached 15000+), which made any static
#: threshold meaningless. The query is now built by ``dedup_query_tokens``:
#: strip HTML/URLs, keep standard CJK words and pure English words only
#: (no digits, punctuation or single letters), dedupe, and take the first
#: ``max_query_tokens``. On that scale true reprints score >= 60 while
#: unrelated articles stay under ~25; the default high threshold is 100 to
#: leave a wide margin above the reprint band.
DEFAULT_DEDUP_QUERY_TOKENS = 10


def get_dedup_config(config: Dict) -> Dict:
    dedup = dict(config.get("dedup") or {})
    return {
        "bm25_low_threshold": float(dedup.get("bm25_low_threshold", 40.0)),
        # Calibrated with the dedup query builder below: unrelated pairs stay
        # below ~25 and true reprints score at least 60.
        "bm25_high_threshold": float(dedup.get("bm25_high_threshold", 60.0)),
        "top_k": int(dedup.get("top_k", 10)),
        "window_hours": int(dedup.get("window_hours", 72)),
        "same_source_bonus": float(dedup.get("same_source_bonus", 2.0)),
        "max_query_tokens": int(dedup.get("max_query_tokens", DEFAULT_DEDUP_QUERY_TOKENS)),
    }


def get_ranking_config(config: Dict) -> Dict:
    """Internal BM25 tuning plus signal score bonuses (§5.5, §5.6).

    These are implementation defaults, not user-facing configuration. Title and
    body get their own ``k1`` / ``b`` pair; the flat ``k1`` / ``b`` keys remain
    accepted for backward compatibility with older local configs.
    """
    ranking = dict(config.get("ranking") or {})
    k1 = float(ranking.get("k1", 1.5))
    b = float(ranking.get("b", 0.75))
    return {
        "title_weight": float(ranking.get("title_weight", 3.0)),
        "k1": k1,
        "b": b,
        "k1_title": float(ranking.get("k1_title", k1)),
        "b_title": float(ranking.get("b_title", b)),
        "k1_content": float(ranking.get("k1_content", k1)),
        "b_content": float(ranking.get("b_content", b)),
        "interest_match_bonus": float(ranking.get("interest_match_bonus", 10.0)),
        "source_weight": float(ranking.get("source_weight", 5.0)),
        "watchlist_bonus": float(ranking.get("watchlist_bonus", 15.0)),
        "recency_bonus": float(ranking.get("recency_bonus", 10.0)),
        "avoid_penalty": float(ranking.get("avoid_penalty", 20.0)),
        "interest_keywords": list(ranking.get("interest_keywords") or []),
        "avoid_keywords": list(ranking.get("avoid_keywords") or []),
        "source_weights": dict(ranking.get("source_weights") or {}),
    }


def get_min_store_score(config: Dict) -> float:
    """The score floor a signal must clear to enter the pool.

    Storing everything means the pool fills with rows no reader will ever
    look at: a 30-point roundup page and a 45-point hobbyist post are not
    signals, and they cost storage, dedup work and BM25 comparisons. The
    floor is applied to the model's own ``score`` — the judgement about the
    content itself — not to ``final_score``, which mixes in source weights
    and recency bonuses that would let a low-quality item buy its way in.
    """
    filter_config = config.get("filter") or {}
    return float(filter_config.get("min_score", DEFAULT_MIN_STORE_SCORE))


def get_push_limits(config: Dict) -> Dict:
    limits = dict((config.get("delivery") or {}).get("push_limits") or {})
    return {
        "hot": int(limits.get("hot", 10)),
        "watchlist": int(limits.get("watchlist", 10)),
        "github": int(limits.get("github", 3)),
        "github_window_days": int(limits.get("github_window_days", 30)),
        # A digest is a curated read, not a firehose. Without a floor the
        # push fills its slots with whatever is left over: a 45-point
        # roundup page and a 58-point hobbyist post are not what a reader
        # opened an investment channel for.
        "min_score": float(limits.get("min_score", DEFAULT_PUSH_MIN_SCORE)),
        # Prevent one active sector from filling the whole digest. A zero
        # value disables the quota; the default keeps the digest readable
        # without sacrificing high-scoring signals from other sectors.
        "max_per_topic": max(0, int(limits.get("max_per_topic", 3) or 0)),
    }


def get_report_config(config: Dict) -> Dict:
    report = dict(config.get("report") or {})
    return {
        "batch_size": int(report.get("batch_size", 200)),
        "content_max_bytes": int(report.get("content_max_bytes", 8192)),
        "path": str(report.get("path", "/api/mcp")).strip() or "/api/mcp",
        "daily_limit_per_user": max(
            0, int(report.get("daily_limit_per_user", DEFAULT_DAILY_SCORING_LIMIT))
        ),
        "quota_timezone": str(
            report.get("quota_timezone", DEFAULT_QUOTA_TIMEZONE)
        ).strip()
        or DEFAULT_QUOTA_TIMEZONE,
        # Shared ingestion key for the separate yield-pool upload path. It is
        # not a manai-pic JWT and grants no user identity: the yield tool only
        # uses it to authorize a full snapshot replacement.
        "reporter_key": str(
            os.environ.get("REPORTER_KEY")
            or os.environ.get("reporter_key")
            or report.get("reporter_key")
            or ""
        ).strip(),
    }


def get_mcp_config(config: Dict) -> Dict:
    """Master MCP transport settings (§6.1).

    ``report.path`` is the full in-app MCP path (``/api/mcp``). The SDK keeps
    ``streamable_http_path`` as ``/mcp``, so the FastAPI mount point is derived
    by stripping that suffix: mounting at ``/api/mcp`` would make Starlette
    307-redirect ``POST /api/mcp`` to ``/api/mcp/`` on every batch.
    """
    mcp = dict(config.get("mcp") or {})
    full_path = get_report_config(config)["path"]
    streamable_path = str(mcp.get("streamable_http_path") or "/mcp").strip() or "/mcp"
    mount_path = full_path
    if mount_path.endswith(streamable_path):
        mount_path = mount_path[: -len(streamable_path)]
    mount_path = mount_path.rstrip("/") or "/"

    return {
        "server_name": str(mcp.get("server_name") or "news-agent-master").strip()
        or "news-agent-master",
        "path": full_path,
        "mount_path": mount_path,
        "streamable_http_path": streamable_path,
        "max_request_body_size": int(mcp.get("max_request_body_size", 4194304)),
        # Empty lists leave DNS rebinding protection off: behind Caddy the Host
        # is the public domain and the SDK default only allows localhost.
        "allowed_hosts": [
            str(h).strip() for h in (mcp.get("allowed_hosts") or []) if str(h).strip()
        ],
        "allowed_origins": [
            str(o).strip() for o in (mcp.get("allowed_origins") or []) if str(o).strip()
        ],
    }


def get_sync_config(config: Dict) -> Dict:
    sync = dict(config.get("sync") or {})
    return {
        "page_size": int(sync.get("page_size", 200)),
        "max_lookback_hours": int(sync.get("max_lookback_hours", 24)),
    }


def get_auth_config(config: Dict) -> Dict:
    auth = dict(config.get("auth") or {})
    return {
        "main_url": str(auth.get("main_url") or "http://127.0.0.1:5678").strip().rstrip("/"),
        "request_timeout": int(auth.get("request_timeout", 10)),
        "membership_database_url": str(auth.get("membership_database_url") or os.environ.get("NEWS_AGENT_MEMBERSHIP_DATABASE_URL") or "").strip(),
        "admins": parse_admin_whitelist(
            os.environ.get("NEWS_AGENT_ADMINS"),
            os.environ.get("NEWS_AGENT_ADMIN_EMAIL"),
        ),
    }


def parse_admin_whitelist(*raw_values: Optional[str]) -> list[str]:
    """Admin identities, each a phone number or an email (§3.1).

    ``NEWS_AGENT_ADMINS`` may be a comma-separated list or a JSON array, so a
    single value and a list are both accepted; the legacy single-value
    ``NEWS_AGENT_ADMIN_EMAIL`` is merged in on top of it.
    """
    admins: list[str] = []
    for raw in raw_values:
        text = str(raw or "").strip()
        if not text:
            continue
        values: list = [text]
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                values = parsed
        elif "," in text:
            values = text.split(",")
        for value in values:
            entry = str(value).strip()
            if entry and entry not in admins:
                admins.append(entry)
    return admins



def get_tokenizer_config(config: Dict) -> Dict:
    tokenizer = dict(config.get("tokenizer") or {})
    return {
        "user_dict": str(tokenizer.get("user_dict") or "").strip(),
        "extra_stopwords": list(tokenizer.get("extra_stopwords") or []),
    }


def get_output_language(config: Dict) -> str:
    """The locale the main ``signal`` table stores. Always one of VALID_LOCALES.

    The top-level key wins because that is where the value has always lived;
    ``llm.output_language`` is the normalised copy the scoring path reads.
    """
    language = str(
        config.get("output_language")
        or (config.get("llm") or {}).get("output_language")
        or "en"
    ).strip().lower()
    if language not in VALID_LOCALES:
        raise ValueError(
            f"invalid output_language {language!r}; expected one of {VALID_LOCALES}"
        )
    return language


def get_content_languages(config: Dict) -> list[str]:
    """Every locale the portal must serve, main-table locale first.

    A single entry means this instance is single-language and no translation
    worker runs at all.
    """
    primary = get_output_language(config)
    configured = config.get("content_languages")
    if not isinstance(configured, list):
        configured = [primary]
    languages = [primary]
    for raw in configured:
        language = str(raw or "").strip().lower()
        if language in VALID_LOCALES and language not in languages:
            languages.append(language)
    return languages


def get_target_locales(config: Dict) -> list[str]:
    """Locales that live in ``signal_translation`` rather than the main table."""
    return [loc for loc in get_content_languages(config) if loc != get_output_language(config)]


def get_translation_config(config: Dict) -> Dict:
    """Master-only translation worker settings.

    A slaver that imports a signal already has it in its own ``output_language``,
    so translating would only produce a locale it never serves; the worker is
    therefore disabled for anything that is not a master.

    GitHub trending display text is authored in the main locale (the scorer
    rewrites the name and summary), so its translation targets are the same
    ``targets`` rule as signals: every served locale except the main one.
    """
    translation = dict(config.get("translation") or {})
    targets = get_target_locales(config)
    enabled = (
        bool(translation.get("enabled", True))
        and is_master(config)
        and bool(targets)
    )
    return {
        "enabled": enabled,
        "targets": targets,
        "concurrency": max(1, int(translation.get("concurrency", 4))),
        "max_retry": max(0, int(translation.get("max_retry", 3))),
        "batch_size": max(1, int(translation.get("batch_size", 10))),
        # Drained more often than the fetch cycle because the queue only grows
        # on insert or on a retry; an empty cycle is a single indexed query.
        "interval_minutes": max(1, int(translation.get("interval_minutes", 10))),
    }


def get_resources_dir(config: Optional[Dict] = None) -> Path:
    return RESOURCES_DIR


def load_config(config_path: str | None = None) -> Dict:
    """Load config.json and normalise the fields the runtime relies on."""
    path = Path(config_path or os.environ.get("NEWS_AGENT_CONFIG") or PROJECT_ROOT / "config.json")
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")

    with open(path, "r", encoding="utf-8") as handle:
        config = json.load(handle)

    config["personal_preferences"] = str(
        config.get("personal_preferences", "")
    ).strip()

    llm = config.setdefault("llm", {})
    llm["personal_preferences"] = config["personal_preferences"]

    config["mode"] = get_mode(config)
    # Normalise the locale config before anything reads it, so an invalid value
    # fails at startup rather than mid-fetch.
    llm["output_language"] = get_output_language(config)
    config["output_language"] = llm["output_language"]
    config["content_languages"] = get_content_languages(config)
    # Fail fast on a broken deployment instead of silently running as local.
    get_database_url(config)
    return config


def resolve_env_value(config: Dict, key_name: str, default: str = "") -> str:
    """Secrets are referenced by env var name, never stored in config.json."""
    if not key_name:
        return default
    value = os.environ.get(key_name)
    return value.strip() if isinstance(value, str) else default
