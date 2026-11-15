"""ORM models for the signal system.

Table shapes follow docs/system-architecture.md §4.1. Index creation that is
backend specific (pg_trgm GIN, FTS5 virtual table) lives in ``schema.py``.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import Base, JSONType, UTCDateTime, utcnow

SINGLETON_ID = "singleton"

# ``signal.reported_by`` for rows this instance collected itself. A reported
# signal carries the verified manai-pic user id instead, so the submitting user
# and the system feed never share a value (§4.1).
SYSTEM_REPORTER = "0"

#: A signal below this score is stored so it can still block a future URL
#: duplicate, but it never appears in a feed, search result, digest or sync
#: payload.  The visibility floor is a product rule, not a configurable
#: ranking knob: reporter rates must mean the same thing on every instance.
VISIBLE_SCORE_FLOOR = 60.0


class Signal(Base):
    """A signal's primary display layer.

    ``title`` / ``summary`` / ``keywords`` are held in the instance's configured
    ``output_language`` only — the language is a global config value, never a
    per-row attribute, so readers know which locale the main table speaks
    without inspecting any column. Other locales live in ``SignalTranslation``.
    ``content`` is always the source-language body and is never translated.
    """

    __tablename__ = "signal"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_urls: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    title: Mapped[str] = mapped_column(Text, default="", nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    title_search: Mapped[str] = mapped_column(Text, default="", nullable=False)
    summary_search: Mapped[str] = mapped_column(Text, default="", nullable=False)
    keywords_search: Mapped[str] = mapped_column(Text, default="", nullable=False)
    content_search: Mapped[str] = mapped_column(Text, default="", nullable=False)
    keywords: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    source_name: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    published_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False
    )
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    final_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    sectors: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    assets: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    direction: Mapped[str] = mapped_column(String(16), default="neutral", nullable=False)
    region: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    origin: Mapped[str] = mapped_column(String(128), default="master", nullable=False)
    reported_by: Mapped[Optional[str]] = mapped_column(
        String(128), default=SYSTEM_REPORTER, nullable=True
    )
    is_github: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    translations: Mapped[List["SignalTranslation"]] = relationship(
        back_populates="signal",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("uq_signal_url", "url", unique=True),
        Index("ix_signal_published_at", "published_at"),
        Index("ix_signal_score", "score"),
        Index("ix_signal_final_score", "final_score"),
        Index("ix_signal_origin_first_seen", "origin", "first_seen_at"),
        Index(
            "ix_signal_first_seen_at",
            "first_seen_at",
            sqlite_where=text("score >= 60"),
            postgresql_where=text("score >= 60"),
        ),
        Index(
            "ix_signal_region_first_seen",
            "region",
            "first_seen_at",
            sqlite_where=text("score >= 60"),
            postgresql_where=text("score >= 60"),
        ),
        Index("ix_signal_reporter_first_seen", "reported_by", "first_seen_at"),
        Index(
            "ix_signal_feed_order",
            "batch_id",
            "final_score",
            "first_seen_at",
            "id",
            sqlite_where=text("score >= 60"),
            postgresql_where=text("score >= 60"),
        ),
        Index("ix_signal_assets_gin", "assets", postgresql_using="gin"),
        Index("ix_signal_sectors_gin", "sectors", postgresql_using="gin"),
    )


class SignalTranslation(Base):
    """One signal's display fields in a locale other than the main table's.

    A row is created as ``pending`` when the signal lands and moves to
    ``completed`` once the translation call returns; a failed translation keeps
    the row so the portal can fall back to the main table and report
    ``fallback: true`` rather than serving an empty title.
    """

    __tablename__ = "signal_translation"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    signal_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("signal.id", ondelete="CASCADE"), nullable=False
    )
    locale: Mapped[str] = mapped_column(String(8), nullable=False)
    title: Mapped[str] = mapped_column(Text, default="", nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    keywords: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    title_search: Mapped[str] = mapped_column(Text, default="", nullable=False)
    summary_search: Mapped[str] = mapped_column(Text, default="", nullable=False)
    keywords_search: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    signal: Mapped["Signal"] = relationship(back_populates="translations")

    __table_args__ = (
        UniqueConstraint("signal_id", "locale", name="uq_signal_translation_locale"),
        Index("ix_signal_translation_status", "status"),
        Index("ix_signal_translation_updated_at", "updated_at"),
        Index("ix_signal_translation_sync", "locale", "status", "updated_at"),
    )


class Watchlist(Base):
    __tablename__ = "watchlist"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    asset: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_class: Mapped[str] = mapped_column(String(32), default="stock", nullable=False)
    aliases: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("owner", "asset", name="uq_watchlist_owner_asset"),
    )


class Sector(Base):
    __tablename__ = "sector"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    keywords: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    sort_weight: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class FetchSource(Base):
    __tablename__ = "fetch_source"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    xml_url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    params: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    etag: Mapped[str] = mapped_column(Text, default="", nullable=False)
    last_modified: Mapped[str] = mapped_column(Text, default="", nullable=False)
    last_fetched_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime, nullable=True
    )
    last_error_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime, nullable=True
    )

    __table_args__ = (
        UniqueConstraint("owner", "kind", "key", name="uq_fetch_source_owner_kind_key"),
    )


class SyncState(Base):
    __tablename__ = "sync_state"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=SINGLETON_ID)
    scope: Mapped[str] = mapped_column(String(32), default="pull", nullable=False)
    last_pulled_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime, nullable=True
    )
    #: Separate cursor for late translations. A signal inserted before
    #: ``last_pulled_at`` can still gain its target-language text afterwards, so
    #: walking signal insertions alone would never deliver it.
    last_translation_pulled_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime, nullable=True
    )
    last_success_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime, nullable=True
    )
    last_failure_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime, nullable=True
    )


class PeerSyncState(Base):
    __tablename__ = "peer_sync_state"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    last_pulled_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    last_success_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    last_failure_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class AppState(Base):
    __tablename__ = "app_state"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=SINGLETON_ID)
    seed_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class DedupDuplicate(Base):
    __tablename__ = "dedup_duplicate"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    candidate_url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    candidate_title: Mapped[str] = mapped_column(Text, default="", nullable=False)
    matched_signal_id: Mapped[str] = mapped_column(String(64), nullable=False)
    match_level: Mapped[str] = mapped_column(String(16), default="url", nullable=False)
    decided_by: Mapped[str] = mapped_column(String(16), default="rule", nullable=False)
    bm25_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (
        Index("ix_dedup_matched_signal_id", "matched_signal_id"),
        Index("ix_dedup_match_level", "match_level"),
        Index("ix_dedup_created_at", "created_at"),
    )


class EnergyLedger(Base):
    __tablename__ = "energy_ledger"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    signal_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    delta: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    balance_after: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (Index("ix_energy_user_created", "user_id", "created_at"),)


class UserProfile(Base):
    __tablename__ = "user_profile"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    username: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    # Public alias shown next to a reported signal.  Generated once on first
    # authenticated projection, then user-editable.  It is deliberately not
    # derived from the email: the portal must never expose that identifier.
    display_name: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    membership: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    energy_cached: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    self_code: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class ReporterScoringUsage(Base):
    """How many candidates entered LLM scoring for one reporter/day.

    The counter is reserved before translation and scoring, after the cheap URL
    dedup gate.  Dates are local calendar dates in the configured quota
    timezone, not UTC dates, so "100 per day" matches the operator's clock.
    """

    __tablename__ = "reporter_scoring_usage"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    usage_date: Mapped[str] = mapped_column(String(10), primary_key=True)
    scored_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class UserPreference(Base):
    """Per-user display preferences keyed by the verified manai-pic user id.

    This is deliberately separate from ``UserProfile``: the profile is a cache
    of the identity provider, while this table is user-owned state.  Regions
    and the display signal filters live together here so browser storage can be
    mirrored without creating another per-purpose preference table.
    """

    __tablename__ = "user_preference"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    regions: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    signal_settings: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class GithubCache(Base):
    """Latest GitHub Trending snapshot produced by the master instance."""

    __tablename__ = "github_cache"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="latest")
    payload: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class GithubPush(Base):
    """GitHub repos that were actually contained in a pushed digest.

    GitHub Trending never enters the signal pool, so URL/BM25 dedup does not
    apply to it. Repetition is suppressed at push time: a repo lands here when
    a digest containing it is pushed, and digest building filters the cache
    against this table within a sliding window (30 days) — after the window a
    repo that trends again is eligible once more.
    """

    __tablename__ = "github_push"

    url: Mapped[str] = mapped_column(String(512), primary_key=True)
    full_name: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    pushed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class SignalPush(Base):
    """Signals that were actually contained in a pushed digest.

    The signal pool itself never shrinks: a pushed signal stays visible in the
    portal, but the same signal must not be pushed again. This table is the
    push-side memory for signals, the counterpart of ``github_push`` for
    repositories. ``signal_id`` is the primary key; ``url_hash`` keeps the
    row useful when a signal is addressed by URL rather than by id.
    """

    __tablename__ = "signal_push"

    signal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    url_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    title: Mapped[str] = mapped_column(Text, default="", nullable=False)
    section: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    locale: Mapped[str] = mapped_column(String(8), default="", nullable=False)
    pushed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (
        Index("ix_signal_push_pushed_at", "pushed_at"),
    )


class GithubTranslation(Base):
    """A cached GitHub repo's display text in a non-English locale.

    Like ``signal_translation`` but keyed by the repo URL: the trending cache
    payload is locale-independent raw English text, and the portal serves the
    same snapshot in every configured language. ``name`` is the localized
    display name for the repo; ``full_name`` stays the raw identifier.
    """

    __tablename__ = "github_translation"

    url: Mapped[str] = mapped_column(String(512), primary_key=True)
    locale: Mapped[str] = mapped_column(String(8), primary_key=True)
    full_name: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    name: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_github_translation_status", "status"),
        Index("ix_github_translation_updated_at", "updated_at"),
    )


class LocalSetting(Base):
    """Slaver-only runtime state such as the stored manai-pic JWT."""

    __tablename__ = "local_setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class YieldPool(Base):
    """A yield opportunity uploaded by a slaver.

    This table is deliberately separate from ``signal``: yield rows are raw
    market observations, not scored editorial signals. They never enter the
    LLM scoring, dedup, sector or delivery pipelines. Identity is the pool's
    natural key ``(pool_name, chain, protocol)`` so a later observation updates
    the existing row instead of creating a second copy.
    """

    __tablename__ = "yield_pool"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    pool_name: Mapped[str] = mapped_column(String(256), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    reference: Mapped[str] = mapped_column(String(128), nullable=False)
    protocol: Mapped[str] = mapped_column(String(128), nullable=False)
    chain: Mapped[str] = mapped_column(String(64), nullable=False)
    tvl_usd: Mapped[float] = mapped_column(Float, nullable=False)
    apy_30d: Mapped[float] = mapped_column(Float, nullable=False)
    apy_today: Mapped[float] = mapped_column(Float, nullable=False)
    expiry_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    reporter: Mapped[str] = mapped_column(String(128), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "pool_name", "chain", "protocol", name="uq_yield_pool_identity"
        ),
        Index("ix_yield_pool_recorded_at", "recorded_at"),
    )


ALL_TABLES: List[type] = [
    Signal,
    SignalTranslation,
    Watchlist,
    Sector,
    FetchSource,
    SyncState,
    PeerSyncState,
    AppState,
    DedupDuplicate,
    EnergyLedger,
    UserProfile,
    UserPreference,
    GithubCache,
    GithubPush,
    GithubTranslation,
    SignalPush,
    LocalSetting,
    YieldPool,
]
