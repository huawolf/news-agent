"""Alembic environment.

The database URL and schema come from the runtime config so that one set of
migrations serves both master (PostgreSQL + news_agent schema) and slaver
(SQLite file).
"""
from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

from src.config import get_database_url, get_schema, load_config
from src.db.base import Base
from src.db import models  # noqa: F401  (register metadata)

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _resolve_url() -> str:
    """Precedence: env override, then an explicit ``sqlalchemy.url`` already set on
    the Alembic config (which is how the app bootstraps), then config.json."""
    override = os.environ.get("NEWS_AGENT_DATABASE_URL", "").strip()
    if override:
        return override

    configured = (config.get_main_option("sqlalchemy.url", "") or "").strip()
    if configured and configured != "driver://user:pass@localhost/dbname":
        return configured

    cfg_path = os.environ.get("NEWS_AGENT_CONFIG", "config.json")
    try:
        cfg = load_config(cfg_path)
    except Exception:
        return configured
    return get_database_url(cfg)


def run_migrations_offline() -> None:
    url = _resolve_url()
    schema = get_schema()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        version_table_schema=schema if url.startswith("postgresql") else None,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _resolve_url()
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = url

    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    schema = get_schema()

    with connectable.connect() as connection:
        is_pg = connection.dialect.name == "postgresql"
        if is_pg:
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
            connection.commit()
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.dialect.default_schema_name = schema

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            version_table_schema=schema if is_pg else None,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()
        # SQLAlchemy 2.0 rolls back on close unless the transaction is committed
        # explicitly. Without this the version stamp is discarded and every
        # restart re-runs the same migration from scratch.
        if connection.in_transaction():
            connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
