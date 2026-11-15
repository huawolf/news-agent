"""Engine, session factory and backend specific schema bootstrap.

Master runs on PostgreSQL inside the ``news_agent`` schema; slaver runs on a
local SQLite file. Both share the same models and repositories, so the only
place that knows about the difference is this module.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator, Optional, Sequence

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from src.db.models import VISIBLE_SCORE_FLOOR

logger = logging.getLogger(__name__)

_engine: Optional[Engine] = None
_session_factory: Optional[sessionmaker] = None
_schema_name = "news_agent"


def _apply_sqlite_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def get_engine(database_url: str, schema: str = "news_agent") -> Engine:
    global _engine, _session_factory, _schema_name

    if _engine is not None:
        return _engine

    _schema_name = schema
    if database_url.startswith("sqlite"):
        engine = create_engine(
            database_url,
            future=True,
            connect_args={"check_same_thread": False},
        )
        _apply_sqlite_pragmas(engine)
    else:
        engine = create_engine(database_url, future=True, pool_pre_ping=True)

        @event.listens_for(engine, "connect")
        def _set_pg_search_path(dbapi_connection, connection_record):  # noqa: ANN001
            # Models are unqualified and live in the agent schema on PostgreSQL,
            # so every connection must resolve them from there. Pinning the path
            # at connect time also keeps the schema that holds ``pg_trgm`` (the
            # extension installs into the first schema on the path) resolvable,
            # which the unqualified ``gin_trgm_ops`` index relies on.
            cursor = dbapi_connection.cursor()
            cursor.execute(f'SET search_path TO "{schema}", public')
            cursor.close()

        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))

    _engine = engine
    _session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return engine


def get_session_factory() -> sessionmaker:
    if _session_factory is None:
        raise RuntimeError("database engine is not initialised; call get_engine() first")
    return _session_factory


def get_schema() -> str:
    return _schema_name


def is_postgres(engine: Optional[Engine] = None) -> bool:
    engine = engine or _engine
    if engine is None:
        return False
    return engine.dialect.name == "postgresql"


def create_backend_indexes(engine: Optional[Engine] = None) -> None:
    """Create recall indexes that cannot be expressed in the model metadata.

    PostgreSQL uses ``pg_trgm`` (ships with the official image); SQLite uses an
    FTS5 virtual table. Neither affects scoring: both only recall candidates,
    the BM25 ranking itself runs in the application layer.
    """
    engine = engine or _engine
    if engine is None:
        raise RuntimeError("database engine is not initialised")

    table = f'"{_schema_name}".signal' if is_postgres(engine) else "signal"

    if is_postgres(engine):
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            conn.execute(
                text(
                    f'CREATE INDEX IF NOT EXISTS ix_signal_title_search_trgm '
                    f'ON {table} USING gin (title_search gin_trgm_ops) '
                    f'WHERE score >= {VISIBLE_SCORE_FLOOR:g}'
                )
            )
            conn.execute(
                text(
                    f'CREATE INDEX IF NOT EXISTS ix_signal_content_search_trgm '
                    f'ON {table} USING gin (content_search gin_trgm_ops) '
                    f'WHERE score >= {VISIBLE_SCORE_FLOOR:g}'
                )
            )
    else:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS signal_fts USING fts5("
                    "url_hash UNINDEXED, title_search, content_search, "
                    "tokenize='unicode61')"
                )
            )
        _rebuild_sqlite_fts(engine)


def _rebuild_sqlite_fts(engine: Engine) -> None:
    """Keep the FTS5 shadow table aligned with the base table."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM signal_fts"))
        conn.execute(
            text(
                "INSERT INTO signal_fts (url_hash, title_search, content_search) "
                f"SELECT url_hash, title_search, content_search FROM signal "
                f"WHERE score >= {VISIBLE_SCORE_FLOOR:g}"
            )
        )


def refresh_sqlite_fts(session: Session, url_hashes: Sequence[str]) -> None:
    """Re-index signals after insert or merge.

    Must run through the caller's session rather than a separate connection:
    SQLite gives the writer an exclusive lock, so opening a second connection
    mid-transaction deadlocks against itself.
    """
    if is_postgres() or not url_hashes:
        return
    for url_hash in url_hashes:
        row = session.execute(
            text(
                "SELECT url_hash, title_search, content_search, score FROM signal "
                "WHERE url_hash = :h"
            ),
            {"h": url_hash},
        ).fetchone()
        session.execute(text("DELETE FROM signal_fts WHERE url_hash = :h"), {"h": url_hash})
        if row is not None and float(row[3] or 0) >= VISIBLE_SCORE_FLOOR:
            session.execute(
                text(
                    "INSERT INTO signal_fts (url_hash, title_search, content_search) "
                    "VALUES (:h, :t, :c)"
                ),
                {"h": row[0], "t": row[1], "c": row[2]},
            )


@contextmanager
def session_scope() -> Iterator[Session]:
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
