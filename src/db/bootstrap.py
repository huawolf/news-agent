"""Startup database bootstrap.

Single entry point used by both the HTTP app lifespan and the CLI:

1. run ``alembic upgrade head`` (schema is owned by migrations),
2. create the backend specific recall indexes,
3. run the idempotent seed.

A failure in any step aborts startup. Running with an empty sector table or an
empty source table looks healthy and silently does nothing, which is far harder
to diagnose than refusing to boot.
"""
from __future__ import annotations

import logging
from typing import Dict

from alembic import command
from alembic.config import Config as AlembicConfig

from src.config import (
    PROJECT_ROOT,
    get_database_url,
    get_mode,
    get_schema,
    get_tokenizer_config,
    load_config,
    redact_database_url,
)
from src.dedup.tokenizer import configure as configure_tokenizer
from src.db.repositories import (
    backfill_display_names,
    get_setting,
    reassign_source_owner,
    reassign_watchlist_owners,
)
from src.db.seed import run_seed
from src.db.session import create_backend_indexes, get_engine, session_scope

logger = logging.getLogger(__name__)

#: Owner used for a slaver's first seed, before a JWT identifies the user.
#: Fetch-source ownership is re-keyed after login; watchlists use a separate
#: stable instance owner so they do not depend on account login state.
PLACEHOLDER_OWNER = "pending"
SLAVER_WATCHLIST_OWNER = "user:0"


def alembic_config(database_url: str) -> AlembicConfig:
    cfg = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    cfg.attributes["configure_logger"] = False
    return cfg


def run_migrations(database_url: str) -> None:
    """Schema is owned by Alembic; ``env.py`` creates the PG schema and enables
    SQLite batch rendering, so this works for both backends unchanged."""
    command.upgrade(alembic_config(database_url), "head")


def resolve_owner(session, config: Dict) -> str:
    """Pick the owner key this process must read and write under.

    A slaver seeds before it has a JWT, so the first boot uses the
    :data:`PLACEHOLDER_OWNER`. Once a token has been saved the real user id
    lives in ``local_setting``; without restoring it here every restart would
    fall back to the placeholder and seed a second catalog nobody reads (§4.3).
    """
    if config.get("mode") == "master":
        return "public"

    configured = str(config.get("owner") or "")
    if configured and configured != PLACEHOLDER_OWNER:
        return configured

    user_id = get_setting(session, "user_id", "")
    return f"user:{user_id}" if user_id else PLACEHOLDER_OWNER


def bootstrap(config: Dict) -> Dict:
    """Migrate, index and seed. Returns the seed statistics."""
    database_url = get_database_url(config)
    schema = get_schema(config)

    run_migrations(database_url)

    engine = get_engine(database_url, schema)
    create_backend_indexes(engine)
    tokenizer = get_tokenizer_config(config)
    configure_tokenizer(tokenizer["user_dict"], tokenizer["extra_stopwords"])

    with session_scope() as session:
        owner = resolve_owner(session, config)
        if get_mode(config) == "slaver":
            reassign_watchlist_owners(session, SLAVER_WATCHLIST_OWNER)
        if owner != PLACEHOLDER_OWNER:
            # An earlier build may have seeded under the placeholder before the
            # user logged in; fold those rows into the real owner first so the
            # seed below sees a single, complete catalog.
            reassign_source_owner(session, PLACEHOLDER_OWNER, owner)
            config["owner"] = owner
        stats = run_seed(session, owner=owner)
        backfill_display_names(session)

    logger.info("database ready: %s (owner=%s)", redact_database_url(database_url), owner)
    return stats


def bootstrap_from_path(config_path: str | None = None) -> Dict:
    return bootstrap(load_config(config_path))
