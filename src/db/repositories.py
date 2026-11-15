"""Repositories.

All business code goes through these functions, which is what keeps the same
code working on PostgreSQL and SQLite. Backend differences are confined to
:mod:`src.db.session`.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import Text, and_, case, cast, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from src.db.models import (
    AppState,
    DedupDuplicate,
    EnergyLedger,
    FetchSource,
    GithubCache,
    GithubPush,
    GithubTranslation,
    LocalSetting,
    ReporterScoringUsage,
    Signal,
    SYSTEM_REPORTER,
    SignalPush,
    SignalTranslation,
    PeerSyncState,
    Sector,
    SyncState,
    UserPreference,
    UserProfile,
    VISIBLE_SCORE_FLOOR,
    Watchlist,
    YieldPool,
)
from src.db.base import utcnow
from src.db.session import is_postgres
from src.dedup.tokenizer import build_search_text, tokenize
from src.llm import normalize_assets, normalize_watchlist_aliases
from src.preferences import clean_signal_settings, normalize_signal_settings
from src.regions import DEFAULT_USER_REGIONS, clean_user_regions, normalize_user_regions

TRANSLATION_PENDING = "pending"
TRANSLATION_COMPLETED = "completed"
TRANSLATION_FAILED = "failed"

# A user query is tokenised the same way as the stored search columns, so a
# multi-word Chinese phrase like "中文标题" still matches the pre-tokenised
# "中文 标题". The cap keeps a pasted paragraph from exploding into a storm of
# ILIKE predicates.
MAX_QUERY_TOKENS = 5


def new_id() -> str:
    return uuid.uuid4().hex


# --------------------------------------------------------------------------- signal


def get_signal_by_url(session: Session, canonical: str, url_hash: str) -> Optional[Signal]:
    """Level-1 lookup: both the canonical ``url`` and merged ``source_urls``."""
    row = session.scalar(
        select(Signal).where(
            or_(Signal.url == canonical, Signal.url_hash == url_hash)
        )
    )
    if row is not None:
        return row

    # source_urls is a JSON list; scan the recent window rather than relying on
    # a backend-specific JSON containment operator.
    candidates = session.scalars(
        select(Signal).where(Signal.source_urls != []).order_by(Signal.first_seen_at.desc()).limit(2000)
    )
    for signal in candidates:
        if canonical in (signal.source_urls or []):
            return signal
    return None


def get_signal_by_id(session: Session, signal_id: str) -> Optional[Signal]:
    return session.get(Signal, signal_id)


def insert_signal(session: Session, values: Dict) -> Signal:
    values = dict(values)
    row_id = values.pop("id", None) or new_id()
    row = Signal(id=row_id, **values)
    session.add(row)
    return row


def merge_source_url(session: Session, signal: Signal, canonical: str) -> None:
    """Append a duplicate's URL to the surviving signal's source list."""
    urls = list(signal.source_urls or [])
    if canonical not in urls:
        urls.append(canonical)
    signal.source_urls = urls


def signals_between(
    session: Session,
    since: datetime,
    until: datetime,
    limit: int = 200,
    offset: int = 0,
    include_github: bool = True,
    visible_only: bool = True,
) -> List[Signal]:
    stmt = (
        select(Signal)
        .where(Signal.first_seen_at >= since, Signal.first_seen_at < until)
        .order_by(Signal.first_seen_at.asc())
        .limit(limit)
        .offset(offset)
    )
    if visible_only:
        stmt = stmt.where(Signal.score >= VISIBLE_SCORE_FLOOR)
    if not include_github:
        stmt = stmt.where(Signal.is_github.is_(False))
    return list(session.scalars(stmt))


def _apply_signal_filters(
    stmt,
    session: Session,
    *,
    sector: Optional[str] = None,
    direction: Optional[str | Sequence[str]] = None,
    min_final_score: Optional[float] = None,
    asset: Optional[str] = None,
    asset_class: Optional[str] = None,
    asset_symbol: Optional[str] = None,
    is_github: Optional[bool] = None,
    owner: Optional[str] = None,
    sectors: Optional[Sequence[str]] = None,
    regions: Optional[Sequence[str]] = None,
    watchlist_only: bool = False,
    watchlist_rows: Optional[Sequence[Watchlist]] = None,
    query: Optional[str] = None,
    locale: Optional[str] = None,
    main_locale: str = "en",
):
    """Apply the shared anonymous-signal filters to a select statement.

    Returns ``None`` when a watchlist-scoped query cannot match anything because
    the scoped watchlist is empty. Keeping this predicate construction in one
    place guarantees that the asset summary and the signal list use identical
    filter semantics.
    """
    # Every reader-facing query shares this predicate.  URL dedup deliberately
    # does not: a hidden row must still block the same URL from being scored
    # again tomorrow.
    stmt = stmt.where(Signal.score >= VISIBLE_SCORE_FLOOR)
    if sector:
        stmt = stmt.where(_json_list_contains(Signal.sectors, sector))
    elif sectors:
        stmt = stmt.where(or_(*[_json_list_contains(Signal.sectors, value) for value in sectors]))
    if regions:
        values = list(dict.fromkeys(str(value) for value in regions if value))
        if values:
            # Region-agnostic sectors (frontier technology, crypto, commodities)
            # carry NULL and must remain visible under every preference.
            stmt = stmt.where(or_(Signal.region.is_(None), Signal.region.in_(values)))
    if direction:
        values = [direction] if isinstance(direction, str) else list(direction)
        stmt = stmt.where(Signal.direction.in_(values))
    if min_final_score is not None:
        stmt = stmt.where(Signal.final_score > min_final_score)
    if is_github is not None:
        stmt = stmt.where(Signal.is_github.is_(is_github))
    if asset_symbol:
        stmt = stmt.where(_json_list_contains(Signal.assets, asset_symbol))
    if watchlist_only or asset or asset_class:
        overlap = _watchlist_overlap(
            session,
            owner=owner,
            asset=asset,
            asset_class=asset_class,
            watchlist_rows=watchlist_rows,
        )
        if overlap is None:
            return None
        stmt = stmt.where(overlap)

    if query and query.strip():
        stmt = stmt.where(_display_text_match(query.strip(), locale, main_locale))
    return stmt


def recent_signals(
    session: Session,
    hours: int = 24,
    sector: Optional[str] = None,
    direction: Optional[str | Sequence[str]] = None,
    min_final_score: Optional[float] = None,
    asset: Optional[str] = None,
    asset_class: Optional[str] = None,
    asset_symbol: Optional[str] = None,
    is_github: Optional[bool] = None,
    owner: Optional[str] = None,
    sectors: Optional[Sequence[str]] = None,
    regions: Optional[Sequence[str]] = None,
    watchlist_only: bool = False,
    query: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    locale: Optional[str] = None,
    main_locale: str = "en",
    order: str = "batch",
) -> List[Signal]:
    """List signals within a caller-selected rolling time window.

    ``locale`` selects which display layer a non-empty ``query`` searches: the
    main table when it matches ``main_locale``, otherwise the translation table.
    ``content`` is always the source-language body, so it is searched in every
    locale; ``assets`` is language independent.

    ``direction`` accepts one value or several, so a caller can request
    "bullish or bearish" (i.e. exclude neutral) in one predicate.
    ``min_final_score`` filters on the displayed score (``final_score``).
    ``asset_symbol`` is an exact, language-independent ``assets`` match.

    ``order`` picks the ranking key. ``batch`` (the default) puts the newest
    batch first and only then the score — the right shape for a feed that
    pages by arrival. ``score`` ranks purely by ``final_score`` — the right
    shape for a digest, where a 45-point signal from the newest batch must
    not outrank a 100-point signal that arrived an hour earlier.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    stmt = select(Signal).where(Signal.first_seen_at >= since)
    stmt = _apply_signal_filters(
        stmt,
        session,
        sector=sector,
        direction=direction,
        min_final_score=min_final_score,
        asset=asset,
        asset_class=asset_class,
        asset_symbol=asset_symbol,
        is_github=is_github,
        owner=owner,
        sectors=sectors,
        regions=regions,
        watchlist_only=watchlist_only,
        query=query,
        locale=locale,
        main_locale=main_locale,
    )
    if stmt is None:
        return []

    if query and query.strip():
        tokens = tokenize(query.strip())[:MAX_QUERY_TOKENS] or [query.strip()]
        asset_tokens = normalize_assets(tokens, limit=MAX_QUERY_TOKENS)
        if asset_tokens:
            asset_match = or_(
                *[_json_list_contains(Signal.assets, asset) for asset in asset_tokens]
            )
            title_match = _localized_display_fields_match(
                tokens, locale, main_locale, ("title_search", "keywords_search")
            )
            stmt = stmt.order_by(
                case((asset_match, 0), (title_match, 1), else_=2)
            )

    if order == "score":
        stmt = stmt.order_by(
            Signal.final_score.desc(),
            Signal.first_seen_at.desc(),
            Signal.id.desc(),
        )
    else:
        stmt = stmt.order_by(
            Signal.batch_id.desc(),
            Signal.final_score.desc(),
            Signal.first_seen_at.desc(),
            Signal.id.desc(),
        )
    stmt = stmt.limit(limit).offset(offset)
    return list(session.scalars(stmt))


def watchlist_asset_counts(
    session: Session,
    owner: str,
    hours: int = 168,
    sector: Optional[str] = None,
    direction: Optional[str | Sequence[str]] = None,
    min_final_score: Optional[float] = None,
    regions: Optional[Sequence[str]] = None,
    query: Optional[str] = None,
    locale: Optional[str] = None,
    main_locale: str = "en",
    watchlist_rows: Optional[Sequence[Watchlist]] = None,
) -> List[Dict[str, Any]]:
    """Count matching signals per watched asset with one signal query.

    The query returns only ``(id, assets)`` for watchlist-matching signals in
    the rolling window. Aggregation happens in Python so the same code path
    works on PostgreSQL and SQLite without issuing one query per asset.
    """
    rows = list(watchlist_rows) if watchlist_rows is not None else list_watchlist(session, owner)
    if not rows:
        return []

    counts = {row.asset: 0 for row in rows}
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    stmt = select(Signal.id, Signal.assets).where(Signal.first_seen_at >= since)
    stmt = _apply_signal_filters(
        stmt,
        session,
        sector=sector,
        direction=direction,
        min_final_score=min_final_score,
        owner=owner,
        regions=regions,
        watchlist_only=True,
        watchlist_rows=rows,
        query=query,
        locale=locale,
        main_locale=main_locale,
    )
    if stmt is not None:
        for _, assets in session.execute(stmt):
            if isinstance(assets, list):
                for asset in set(assets) & counts.keys():
                    counts[asset] += 1

    return [
        {
            "asset": row.asset,
            "asset_class": row.asset_class,
            "count_7d": counts[row.asset],
        }
        for row in rows
    ]


def recent_titles_for_dedup(
    session: Session,
    *,
    min_final_score: float,
    limit: int,
) -> List[Dict[str, Any]]:
    """The newest high-scoring titles used as LLM duplicate-reference context."""
    rows = session.scalars(
        select(Signal)
        .where(
            Signal.is_github.is_(False),
            Signal.score >= VISIBLE_SCORE_FLOOR,
            Signal.final_score > min_final_score,
        )
        .order_by(Signal.first_seen_at.desc(), Signal.id.desc())
        .limit(limit)
    )
    return [
        {
            "id": row.id,
            "title": row.title,
            "first_seen_at": row.first_seen_at.isoformat().replace("+00:00", "Z"),
        }
        for row in rows
    ]


def _display_text_match(query: str, locale: Optional[str], main_locale: str):
    """Build the user-search predicate for one locale.

    The query is tokenised with the same tokenizer that produced the stored
    search columns, then the first :data:`MAX_QUERY_TOKENS` tokens are ANDed —
    every token must appear in one of the searchable columns. This is what makes
    a multi-word Chinese query work: "中文标题" becomes ``中文`` AND ``标题`` and
    still matches a row whose ``title_search`` holds the pre-tokenised
    ``"中文 标题"``.

    Searchable text is the locale's title / summary / keywords plus the
    language-independent body and assets. The main table is searched only when
    the requested locale is the main locale — deliberately no fallback to the
    main layer's text for a target locale, because that would return rows whose
    visible title the user cannot read.
    """
    tokens = tokenize(query)[:MAX_QUERY_TOKENS]
    if not tokens:
        # A query made only of stopwords or punctuation has nothing to AND on;
        # fall back to the raw substring so it behaves like a plain lookup
        # instead of degenerating into "match every row".
        tokens = [query]

    def covers(token: str):
        pattern = f"%{token}%"
        body_and_assets = or_(
            Signal.content_search.ilike(pattern),
            Signal.assets.cast(Text).ilike(pattern),
        )
        if not locale or locale == main_locale:
            return or_(
                Signal.title_search.ilike(pattern),
                Signal.summary_search.ilike(pattern),
                Signal.keywords_search.ilike(pattern),
                body_and_assets,
            )
        translated = (
            select(1)
            .select_from(SignalTranslation)
            .where(
                SignalTranslation.signal_id == Signal.id,
                SignalTranslation.locale == locale,
                SignalTranslation.status == TRANSLATION_COMPLETED,
                or_(
                    SignalTranslation.title_search.ilike(pattern),
                    SignalTranslation.summary_search.ilike(pattern),
                    SignalTranslation.keywords_search.ilike(pattern),
                ),
            )
            .exists()
        )
        return or_(translated, body_and_assets)

    return and_(*[covers(token) for token in tokens])


def _localized_display_fields_match(
    tokens: Sequence[str],
    locale: Optional[str],
    main_locale: str,
    fields: Sequence[str],
):
    """Match every query token against one localized set of display fields."""
    if not locale or locale == main_locale:
        return and_(
            *[
                or_(*[getattr(Signal, field).ilike(f"%{token}%") for field in fields])
                for token in tokens
            ]
        )

    return (
        select(1)
        .select_from(SignalTranslation)
        .where(
            SignalTranslation.signal_id == Signal.id,
            SignalTranslation.locale == locale,
            SignalTranslation.status == TRANSLATION_COMPLETED,
            and_(
                *[
                    or_(
                        *[
                            getattr(SignalTranslation, field).ilike(f"%{token}%")
                            for field in fields
                        ]
                    )
                    for token in tokens
                ]
            ),
        )
        .exists()
    )


def _json_list_contains(column, value: str):
    """``value in column`` for a JSON array column, on either backend.

    The column type is ``JSON`` with a ``JSONB`` variant, so ``contains()``
    resolves to the generic string ``LIKE`` comparator (whose JSONB cast fails)
    instead of JSONB containment; the ``@>`` operator has to be emitted
    explicitly. SQLite stores the array as TEXT, so the comparison goes through
    ``json_each``.

    On PostgreSQL the right-hand operand must be a *Python* list, not a
    pre-serialised ``json.dumps`` string: psycopg3 adapts a ``str`` by running
    ``json.dumps`` on it, so passing ``'["宏观经济"]'`` would bind the JSON
    string ``"[\"宏观经济\"]"`` and ``@>`` would never match. Handing over the
    list lets the driver serialise it exactly once.
    """
    if is_postgres():
        return column.op("@>")(cast([value], JSONB))
    items = func.json_each(column).table_valued("key", "value").alias()
    return select(1).select_from(items).where(items.c.value == value).exists()


def _watchlist_overlap(
    session: Session,
    owner: Optional[str] = None,
    asset: Optional[str] = None,
    asset_class: Optional[str] = None,
    watchlist_rows: Optional[Sequence[Watchlist]] = None,
):
    """Match signals whose ``assets`` intersect a watchlist.

    There is no asset master table, so matching is a normalised equality test on
    the upper-case asset spelling — which is exactly why the spelling has to be
    uniform. ``owner`` scopes the watchlist; ``None`` means every owner's list,
    which is what the shared pool needs.

    Returns ``None`` when the scoped watchlist is empty, so callers can short
    circuit instead of matching against an empty asset set.
    """
    if watchlist_rows is not None:
        assets = [
            row.asset
            for row in watchlist_rows
            if (not asset or row.asset == asset)
            and (not asset_class or row.asset_class == asset_class)
        ]
    else:
        stmt = select(Watchlist)
        if owner is not None:
            stmt = stmt.where(Watchlist.owner == owner)
        if asset:
            stmt = stmt.where(Watchlist.asset == asset)
        if asset_class:
            stmt = stmt.where(Watchlist.asset_class == asset_class)
        assets = [row.asset for row in session.scalars(stmt)]
    if not assets:
        return None
    return or_(*[_json_list_contains(Signal.assets, item) for item in assets])


def search_signals(
    session: Session,
    query: str,
    hours: int = 24,
    limit: int = 50,
    offset: int = 0,
    locale: Optional[str] = None,
    main_locale: str = "en",
) -> List[Signal]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    if not query.strip():
        stmt = select(Signal).where(
            Signal.first_seen_at >= since,
            Signal.score >= VISIBLE_SCORE_FLOOR,
        )
    else:
        stmt = select(Signal).where(
            Signal.first_seen_at >= since,
            Signal.score >= VISIBLE_SCORE_FLOOR,
            _display_text_match(query.strip(), locale, main_locale),
        )
    stmt = stmt.order_by(Signal.first_seen_at.desc()).limit(limit).offset(offset)
    return list(session.scalars(stmt))


def recall_candidates(
    session: Session,
    tokens: Sequence[str],
    since: datetime,
    limit: int = 200,
) -> List[Signal]:
    """Backend specific candidate recall for the BM25 stage.

    Only recalls; the ranking itself is computed by :mod:`src.dedup.bm25`, so both
    backends must return the same rows for the same token set.
    """
    if not tokens:
        return []

    tokens = list(tokens)[:20]
    if is_postgres():
        clauses = [
            or_(Signal.title_search.ilike(f"%{t}%"), Signal.content_search.ilike(f"%{t}%"))
            for t in tokens
        ]
        stmt = (
            select(Signal)
            .where(
                Signal.first_seen_at >= since,
                Signal.score >= VISIBLE_SCORE_FLOOR,
                or_(*clauses),
            )
            .order_by(Signal.first_seen_at.desc())
            .limit(limit)
        )
        return list(session.scalars(stmt))

    match_expr = " OR ".join(f'"{t}"' for t in tokens)
    try:
        hits = session.execute(
            text("SELECT url_hash FROM signal_fts WHERE signal_fts MATCH :q"),
            {"q": match_expr},
        ).all()
    except Exception:
        return []
    hashes = [row[0] for row in hits]
    if not hashes:
        return []
    stmt = (
        select(Signal)
        .where(
            Signal.url_hash.in_(hashes),
            Signal.first_seen_at >= since,
            Signal.score >= VISIBLE_SCORE_FLOOR,
        )
        .order_by(Signal.first_seen_at.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


def window_documents(
    session: Session,
    since: datetime,
    limit: int = 2000,
) -> List[Signal]:
    """Every signal inside the dedup window, used for BM25 corpus statistics.

    Only the pre-tokenised columns are needed; idf and the average field lengths
    must describe the whole window, otherwise scoring a recalled subset against
    its own statistics collapses every score.
    """
    stmt = (
        select(Signal)
        .where(
            Signal.first_seen_at >= since,
            Signal.score >= VISIBLE_SCORE_FLOOR,
        )
        .order_by(Signal.first_seen_at.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


def signals_in_window(session: Session, hours: int) -> List[Signal]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    return list(
        session.scalars(
            select(Signal)
            .where(
                Signal.first_seen_at >= since,
                Signal.score >= VISIBLE_SCORE_FLOOR,
            )
            .order_by(Signal.first_seen_at.desc())
        )
    )


# ------------------------------------------------------------------- translations


def get_translation(
    session: Session, signal_id: str, locale: str
) -> Optional[SignalTranslation]:
    return session.scalar(
        select(SignalTranslation).where(
            SignalTranslation.signal_id == signal_id,
            SignalTranslation.locale == locale,
        )
    )


def translations_for(
    session: Session, signal_ids: Sequence[str], locale: str
) -> Dict[str, SignalTranslation]:
    """Completed translations keyed by signal id, for one batch of listings."""
    ids = [value for value in signal_ids if value]
    if not ids or not locale:
        return {}
    rows = session.scalars(
        select(SignalTranslation).where(
            SignalTranslation.signal_id.in_(ids),
            SignalTranslation.locale == locale,
            SignalTranslation.status == TRANSLATION_COMPLETED,
        )
    )
    return {row.signal_id: row for row in rows}


def queue_translation(
    session: Session,
    signal_id: str,
    locale: str,
    title: str = "",
    summary: str = "",
    keywords: Optional[Sequence[str]] = None,
    status: str = TRANSLATION_PENDING,
) -> SignalTranslation:
    """Create or reset the target-locale row for a signal.

    A signal reported in a locale other than the main one arrives with that
    locale's text already filled in, so the row is inserted as ``completed``
    instead of queueing a translation that would only re-derive it.
    """
    row = get_translation(session, signal_id, locale)
    values = {
        "title": title,
        "summary": summary,
        "keywords": list(keywords or []),
        "title_search": build_search_text(title),
        "summary_search": build_search_text(summary),
        "keywords_search": build_search_text(" ".join(keywords or [])),
        "status": status,
        "retry_count": 0,
        "last_error": "",
        "updated_at": utcnow(),
    }
    if row is None:
        row = SignalTranslation(
            id=new_id(), signal_id=signal_id, locale=locale, created_at=utcnow(), **values
        )
        session.add(row)
        return row
    for key, value in values.items():
        setattr(row, key, value)
    return row


def pending_translations(
    session: Session, locales: Sequence[str], max_retry: int, limit: int = 50
) -> List[SignalTranslation]:
    """Retryable translation work: anything unfinished and still under budget.

    ``failed`` rows come back for another attempt; once ``retry_count`` exceeds
    ``max_retry`` they drop out of the queue and the portal keeps serving the
    main-table text.
    """
    locales = [loc for loc in locales if loc]
    if not locales:
        return []
    return list(
        session.scalars(
            select(SignalTranslation)
            .where(
                SignalTranslation.locale.in_(locales),
                SignalTranslation.status.in_(
                    [TRANSLATION_PENDING, TRANSLATION_FAILED]
                ),
                SignalTranslation.retry_count <= max_retry,
            )
            .order_by(SignalTranslation.updated_at.asc())
            .limit(limit)
        )
    )


def mark_translation_failed(session: Session, row: SignalTranslation, error: str) -> None:
    """Record a failed attempt; the row stays for the portal's fallback path."""
    row.status = TRANSLATION_FAILED
    row.retry_count = (row.retry_count or 0) + 1
    row.last_error = error[:2000]
    row.updated_at = utcnow()


def mark_translation_completed(
    session: Session,
    row: SignalTranslation,
    title: str,
    summary: str,
    keywords: Sequence[str],
) -> None:
    row.title = title
    row.summary = summary
    row.keywords = list(keywords)
    row.title_search = build_search_text(title)
    row.summary_search = build_search_text(summary)
    row.keywords_search = build_search_text(" ".join(keywords))
    row.status = TRANSLATION_COMPLETED
    row.retry_count = 0
    row.last_error = ""
    row.updated_at = utcnow()


def translations_updated_between(
    session: Session,
    since: datetime,
    until: datetime,
    locale: str,
    limit: int = 200,
    offset: int = 0,
) -> List[SignalTranslation]:
    """Translations finished inside a window — the sync cursor for late text.

    A signal inserted before ``since`` can still gain its target-language text
    afterwards, so the incremental feed has to walk translation updates as well
    as first insertions.
    """
    stmt = (
        select(SignalTranslation)
        .where(
            SignalTranslation.locale == locale,
            SignalTranslation.status == TRANSLATION_COMPLETED,
            SignalTranslation.updated_at >= since,
            SignalTranslation.updated_at < until,
        )
        .order_by(SignalTranslation.updated_at.asc())
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(stmt))


# --------------------------------------------------------------------------- sector


def list_sectors(session: Session, enabled_only: bool = False) -> List[Sector]:
    stmt = select(Sector)
    if enabled_only:
        stmt = stmt.where(Sector.enabled.is_(True))
    return list(session.scalars(stmt.order_by(Sector.sort_order.asc(), Sector.name.asc())))


def get_sector(session: Session, name: str) -> Optional[Sector]:
    return session.scalar(select(Sector).where(Sector.name == name))


def enabled_sector_specs(session: Session) -> List[Dict]:
    """Return enabled sectors in the shape the scoring prompt consumes.

    The scoring path never reads the seed file, so a sector disabled in the
    database disappears from both the prompt and the allowed-name filter.
    """
    return [
        {"name": row.name, "keywords": list(row.keywords or [])}
        for row in list_sectors(session, enabled_only=True)
    ]


# --------------------------------------------------------------------------- source


def list_sources(
    session: Session,
    owner: str,
    kinds: Optional[Sequence[str]] = None,
    enabled_only: bool = False,
) -> List[FetchSource]:
    stmt = select(FetchSource).where(FetchSource.owner == owner)
    if kinds:
        stmt = stmt.where(FetchSource.kind.in_(list(kinds)))
    if enabled_only:
        stmt = stmt.where(FetchSource.enabled.is_(True))
    return list(session.scalars(stmt.order_by(FetchSource.kind.asc(), FetchSource.title.asc())))


def reassign_source_owner(session: Session, old_owner: str, new_owner: str) -> int:
    """Move seeded sources to the real user owner after login.

    A slaver seeds before it has a JWT, so the rows land under the placeholder
    owner. Re-seeding under the real owner would duplicate the whole catalog.

    ``fetch_source`` is unique on ``(owner, kind, key)``, so a bulk ``UPDATE``
    blows up as soon as a previous login already created the same key under the
    real owner — which is exactly what happens when a user logs in twice or when
    an older build seeded both. Rows are therefore merged one by one: the
    existing ``new_owner`` row wins (it may carry user edits and a fetch
    fingerprint), the placeholder duplicate is dropped.
    """
    if old_owner == new_owner:
        return 0

    placeholders = list(
        session.scalars(select(FetchSource).where(FetchSource.owner == old_owner))
    )
    if not placeholders:
        return 0

    existing = {
        (row.kind, row.key): row
        for row in session.scalars(
            select(FetchSource).where(FetchSource.owner == new_owner)
        )
    }

    moved = 0
    for row in placeholders:
        clash = existing.get((row.kind, row.key))
        if clash is None:
            row.owner = new_owner
            existing[(row.kind, row.key)] = row
            moved += 1
            continue
        # Keep the richer of the two fingerprints so a merge does not force a
        # full re-fetch on the next cycle.
        clash.etag = clash.etag or row.etag
        clash.last_modified = clash.last_modified or row.last_modified
        session.delete(row)
    session.flush()
    return moved


def get_source(session: Session, owner: str, kind: str, key: str) -> Optional[FetchSource]:
    return session.scalar(
        select(FetchSource).where(
            FetchSource.owner == owner, FetchSource.kind == kind, FetchSource.key == key
        )
    )


def update_source_fingerprint(
    session: Session,
    source_id: str,
    *,
    etag: Optional[str] = None,
    last_modified: Optional[str] = None,
    fetched: Optional[bool] = None,
    fetched_at: Optional[datetime] = None,
    error: Optional[bool] = None,
) -> None:
    values: Dict = {}
    if etag is not None:
        values["etag"] = etag
    if last_modified is not None:
        values["last_modified"] = last_modified
    now = datetime.now(timezone.utc)
    if fetched:
        values["last_fetched_at"] = fetched_at or now
    if error:
        values["last_error_at"] = now
    if not values:
        return
    session.execute(update(FetchSource).where(FetchSource.id == source_id).values(**values))


def create_source(session: Session, owner: str, kind: str, key: str, **kwargs) -> FetchSource:
    row = FetchSource(
        id=new_id(),
        owner=owner,
        kind=kind,
        key=key,
        title=kwargs.get("title", ""),
        xml_url=kwargs.get("xml_url", ""),
        enabled=kwargs.get("enabled", True),
        params=kwargs.get("params", {}),
    )
    session.add(row)
    return row


def delete_source(session: Session, owner: str, kind: str, key: str) -> bool:
    row = get_source(session, owner, kind, key)
    if row is None:
        return False
    session.delete(row)
    return True


# --------------------------------------------------------------------------- sync state


def get_sync_state(session: Session) -> SyncState:
    row = session.get(SyncState, "singleton")
    if row is None:
        row = SyncState(id="singleton", scope="pull")
        session.add(row)
        session.flush()
    return row


def advance_sync_state(
    session: Session,
    pulled_at: datetime,
    translation_pulled_at: Optional[datetime] = None,
) -> None:
    """Only called after every page has been pulled successfully.

    The two cursors advance independently because they walk different tables:
    a translation can complete long after its signal was first pulled, and
    advancing only the signal cursor would strand it forever.
    """
    row = get_sync_state(session)
    now = datetime.now(timezone.utc)
    row.last_pulled_at = pulled_at
    if translation_pulled_at is not None:
        row.last_translation_pulled_at = translation_pulled_at
    row.last_success_at = now
    row.last_failure_at = None


def mark_sync_failure(session: Session) -> None:
    row = get_sync_state(session)
    row.last_failure_at = datetime.now(timezone.utc)


def record_peer_sync(session: Session, user_id: str, pulled_at: Optional[datetime], success: bool = True) -> PeerSyncState:
    row = session.get(PeerSyncState, user_id)
    now = datetime.now(timezone.utc)
    if row is None:
        row = PeerSyncState(user_id=user_id)
        session.add(row)
    row.updated_at = now
    if success:
        row.last_pulled_at = pulled_at
        row.last_success_at = now
        row.last_failure_at = None
    else:
        row.last_failure_at = now
    return row


def list_peer_sync_states(session: Session) -> List[PeerSyncState]:
    return list(session.scalars(select(PeerSyncState).order_by(PeerSyncState.updated_at.desc())))


# --------------------------------------------------------------------------- app state


def get_seed_version(session: Session) -> int:
    row = session.get(AppState, "singleton")
    return row.seed_version if row else 0


def set_seed_version(session: Session, version: int) -> None:
    row = session.get(AppState, "singleton")
    if row is None:
        session.add(AppState(id="singleton", seed_version=version))
    else:
        row.seed_version = version


# --------------------------------------------------------------------------- dedup archive


def record_duplicate(
    session: Session,
    *,
    candidate_url: Optional[str],
    candidate_title: str,
    matched_signal_id: str,
    match_level: str,
    decided_by: str,
    bm25_score: float = 0.0,
    confidence: float = 0.0,
    reason: str = "",
) -> bool:
    """Archive one dedup hit. Idempotent per (url, signal, level): re-fetched
    duplicates of an already-archived URL are skipped so the archive does not
    grow once per fetch round. Returns ``True`` when a row was inserted."""
    if candidate_url:
        seen = (
            session.query(DedupDuplicate.id)
            .filter(
                DedupDuplicate.candidate_url == candidate_url,
                DedupDuplicate.matched_signal_id == matched_signal_id,
                DedupDuplicate.match_level == match_level,
            )
            .first()
        )
        if seen is not None:
            return False
    session.add(
        DedupDuplicate(
            id=new_id(),
            candidate_url=candidate_url or "",
            candidate_title=candidate_title,
            matched_signal_id=matched_signal_id,
            match_level=match_level,
            decided_by=decided_by,
            bm25_score=bm25_score,
            confidence=confidence,
            reason=reason,
        )
    )
    return True


def dedup_stats(session: Session, hours: int = 168) -> Dict:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = session.execute(
        select(DedupDuplicate.match_level, DedupDuplicate.decided_by, DedupDuplicate.bm25_score)
        .where(DedupDuplicate.created_at >= since)
    ).all()

    total = len(rows)
    by_level: Dict[str, int] = {}
    fallback = 0
    score_sum = 0.0
    for match_level, decided_by, bm25_score in rows:
        by_level[match_level] = by_level.get(match_level, 0) + 1
        if decided_by == "fallback":
            fallback += 1
        score_sum += float(bm25_score or 0.0)

    return {
        "total": total,
        "by_match_level": by_level,
        "fallback_count": fallback,
        "fallback_ratio": (fallback / total) if total else 0.0,
        "avg_bm25_score": (score_sum / total) if total else 0.0,
    }


def list_duplicates(
    session: Session,
    hours: int = 168,
    match_level: Optional[str] = None,
    signal_id: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> List[DedupDuplicate]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    stmt = select(DedupDuplicate).where(DedupDuplicate.created_at >= since)
    if match_level:
        stmt = stmt.where(DedupDuplicate.match_level == match_level)
    if signal_id:
        stmt = stmt.where(DedupDuplicate.matched_signal_id == signal_id)
    return list(
        session.scalars(
            stmt.order_by(DedupDuplicate.created_at.desc()).limit(limit).offset(offset)
        )
    )


# --------------------------------------------------------------------------- energy


ENERGY_PULL_NEW = -0.1
ENERGY_PULL_DUPLICATE = 0.0
ENERGY_REPORT_NEW = 1.0


def record_energy(
    session: Session,
    user_id: str,
    event_type: str,
    delta: float,
    signal_id: Optional[str] = None,
) -> float:
    """Append one ledger row and return the resulting balance.

    Accounting only: the balance may go negative, which means "owed" in this
    bookkeeping sense, not an actual negative quota.
    """
    balance = energy_balance(session, user_id) + float(delta)
    session.add(
        EnergyLedger(
            id=new_id(),
            user_id=user_id,
            event_type=event_type,
            signal_id=signal_id,
            delta=float(delta),
            balance_after=balance,
        )
    )
    return balance


def energy_balance(session: Session, user_id: str) -> float:
    # Sum of deltas, not max(balance_after): the balance can go up and down, so
    # the maximum is not the latest value.
    value = session.scalar(
        select(func.coalesce(func.sum(EnergyLedger.delta), 0.0)).where(
            EnergyLedger.user_id == user_id
        )
    )
    return float(value or 0.0)


def list_energy_entries(
    session: Session, user_id: Optional[str] = None, limit: int = 100, offset: int = 0
) -> List[EnergyLedger]:
    stmt = select(EnergyLedger)
    if user_id:
        stmt = stmt.where(EnergyLedger.user_id == user_id)
    return list(
        session.scalars(
            stmt.order_by(EnergyLedger.created_at.desc()).limit(limit).offset(offset)
        )
    )


# --------------------------------------------------------------------------- user profile


def upsert_user_profile(
    session: Session,
    user_id: str,
    username: str = "",
    display_name: Optional[str] = None,
    membership: str = "",
    energy_cached: Optional[float] = None,
    self_code: str = "",
    is_admin: bool = False,
    generate_display_name: bool = True,
) -> UserProfile:
    row = session.get(UserProfile, user_id)
    now = datetime.now(timezone.utc)
    if row is None:
        name = (display_name or "").strip()
        if not name and generate_display_name:
            name = new_display_name()
        row = UserProfile(
            user_id=user_id,
            username=username,
            display_name=name,
            membership=membership,
            energy_cached=energy_cached,
            self_code=self_code,
            is_admin=is_admin,
            last_seen_at=now,
        )
        session.add(row)
    else:
        row.username = username or row.username
        name = (display_name or "").strip()
        if name:
            row.display_name = name
        elif not row.display_name and generate_display_name:
            row.display_name = new_display_name()
        row.membership = membership or row.membership
        if energy_cached is not None:
            row.energy_cached = energy_cached
        row.self_code = self_code or row.self_code
        row.is_admin = is_admin
        row.last_seen_at = now
    return row


def list_user_profiles(session: Session, limit: int = 100, offset: int = 0) -> List[UserProfile]:
    return list(
        session.scalars(
            select(UserProfile).order_by(UserProfile.last_seen_at.desc()).limit(limit).offset(offset)
        )
    )


def new_display_name() -> str:
    """A neutral public alias; never derived from email or the manai-pic user id."""
    return f"investor_{secrets.token_hex(4)}"


def get_user_profile(session: Session, user_id: str) -> Optional[UserProfile]:
    return session.get(UserProfile, user_id)


def profiles_for(session: Session, user_ids: Sequence[str]) -> Dict[str, UserProfile]:
    ids = [str(value) for value in user_ids if value and str(value) != SYSTEM_REPORTER]
    if not ids:
        return {}
    rows = session.scalars(select(UserProfile).where(UserProfile.user_id.in_(ids)))
    return {row.user_id: row for row in rows}


def public_display_name(user_id: str, profile: Optional[UserProfile]) -> str:
    if user_id == SYSTEM_REPORTER:
        return ""
    name = (profile.display_name if profile is not None else "").strip()
    return name or f"user_{user_id}"


def set_user_display_name(session: Session, user_id: str, display_name: str) -> UserProfile:
    row = session.get(UserProfile, user_id)
    now = datetime.now(timezone.utc)
    if row is None:
        row = UserProfile(user_id=user_id, display_name=display_name, last_seen_at=now)
        session.add(row)
    else:
        row.display_name = display_name
        row.last_seen_at = now
    return row


def backfill_display_names(session: Session) -> int:
    """Give legacy profiles and historical reporters a public name.

    Runs once per startup.  It never overwrites an existing name; a profile is
    created only when a reported signal references a user that predates the
    ``user_profile`` projection.
    """
    changed = 0
    profiles = list(
        session.scalars(
            select(UserProfile).where(
                or_(UserProfile.display_name == "", UserProfile.display_name.is_(None))
            )
        )
    )
    for row in profiles:
        row.display_name = new_display_name()
        changed += 1

    existing = {row.user_id for row in session.scalars(select(UserProfile))}
    reporter_ids = session.scalars(
        select(Signal.reported_by)
        .where(Signal.reported_by.is_not(None), Signal.reported_by != SYSTEM_REPORTER)
        .distinct()
    )
    for user_id in reporter_ids:
        value = str(user_id or "").strip()
        if not value or value in existing:
            continue
        session.add(UserProfile(user_id=value, display_name=new_display_name()))
        existing.add(value)
        changed += 1
    return changed


def reporter_statistics(session: Session, user_id: str) -> Dict[str, Any]:
    """Public reputation stats for one reporter.

    ``scored_total`` includes hidden rows on purpose: every row that reached
    the scorer cost an LLM request, so it belongs in the denominator.  Both
    rates stay ``None`` until ten rows exist; a 2/2 start must not look better
    than a long track record.
    """
    scored_total = int(
        session.scalar(
            select(func.count())
            .select_from(Signal)
            .where(Signal.reported_by == user_id)
        )
        or 0
    )
    displayable_total = int(
        session.scalar(
            select(func.count())
            .select_from(Signal)
            .where(
                Signal.reported_by == user_id,
                Signal.score >= VISIBLE_SCORE_FLOOR,
            )
        )
        or 0
    )
    high_score = int(
        session.scalar(
            select(func.count())
            .select_from(Signal)
            .where(
                Signal.reported_by == user_id,
                Signal.score >= VISIBLE_SCORE_FLOOR,
                Signal.final_score > 80,
            )
        )
        or 0
    )
    first_reported, last_reported = session.execute(
        select(func.min(Signal.first_seen_at), func.max(Signal.first_seen_at)).where(
            Signal.reported_by == user_id
        )
    ).one()
    recent_rows = session.execute(
        select(Signal.score, Signal.final_score)
        .where(Signal.reported_by == user_id)
        .order_by(Signal.first_seen_at.desc(), Signal.id.desc())
        .limit(10)
    ).all()
    recent_scored_total = len(recent_rows)
    recent_high_score = sum(
        1
        for score, final_score in recent_rows
        if float(score or 0) >= VISIBLE_SCORE_FLOOR
        and float(final_score or 0) > 80
    )

    return {
        "scored_total": scored_total,
        "displayable_total": displayable_total,
        "high_score": high_score,
        "high_score_rate": (
            round(high_score / scored_total, 4) if scored_total >= 10 else None
        ),
        "recent_scored_total": recent_scored_total,
        "recent_high_score": recent_high_score,
        "recent_high_score_rate": (
            round(recent_high_score / recent_scored_total, 4)
            if recent_scored_total >= 10
            else None
        ),
        "first_reported_at": first_reported,
        "last_reported_at": last_reported,
    }


def reporter_signals(
    session: Session,
    user_id: str,
    *,
    limit: int = 50,
    offset: int = 0,
    sector: Optional[str] = None,
    direction: Optional[str | Sequence[str]] = None,
    min_final_score: Optional[float] = None,
    asset_symbol: Optional[str] = None,
    sectors: Optional[Sequence[str]] = None,
    regions: Optional[Sequence[str]] = None,
    query: Optional[str] = None,
    locale: Optional[str] = None,
    main_locale: str = "en",
) -> List[Signal]:
    """Full public history for one reporter, not capped by the portal's 7 days."""
    stmt = select(Signal).where(Signal.reported_by == user_id)
    stmt = _apply_signal_filters(
        stmt,
        session,
        sector=sector,
        direction=direction,
        min_final_score=min_final_score,
        asset_symbol=asset_symbol,
        sectors=sectors,
        regions=regions,
        query=query,
        locale=locale,
        main_locale=main_locale,
    )
    if stmt is None:
        return []
    stmt = stmt.order_by(
        Signal.first_seen_at.desc(),
        Signal.id.desc(),
    ).limit(limit).offset(offset)
    return list(session.scalars(stmt))


# ------------------------------------------------------------------ reporter quota


def scoring_usage(session: Session, user_id: str, usage_date: str) -> int:
    value = session.scalar(
        select(ReporterScoringUsage.scored_count).where(
            ReporterScoringUsage.user_id == user_id,
            ReporterScoringUsage.usage_date == usage_date,
        )
    )
    return int(value or 0)


def reserve_scoring_slots(
    session: Session, user_id: str, usage_date: str, limit: int, count: int
) -> int:
    """Atomically reserve up to ``count`` scoring slots for one reporter/day.

    The row is updated under a ``scored_count < limit`` predicate, so concurrent
    slaver batches cannot both spend the last slot.  One statement per reserved
    candidate is deliberate: a reporter is capped at 100/day, making the loop
    tiny while keeping the cap exact on both SQLite and PostgreSQL.
    """
    if limit <= 0 or count <= 0 or not user_id:
        return 0
    insert_factory = pg_insert if is_postgres() else sqlite_insert
    reserved = 0
    for _ in range(int(count)):
        if reserved >= limit:
            break
        session.execute(
            insert_factory(ReporterScoringUsage)
            .values(
                user_id=user_id,
                usage_date=usage_date,
                scored_count=0,
                updated_at=utcnow(),
            )
            .on_conflict_do_nothing(index_elements=["user_id", "usage_date"])
        )
        result = session.execute(
            update(ReporterScoringUsage)
            .where(
                ReporterScoringUsage.user_id == user_id,
                ReporterScoringUsage.usage_date == usage_date,
                ReporterScoringUsage.scored_count < limit,
            )
            .values(
                scored_count=ReporterScoringUsage.scored_count + 1,
                updated_at=utcnow(),
            )
        )
        if result.rowcount != 1:
            break
        reserved += 1
    return reserved


def get_user_regions(session: Session, user_id: str) -> List[str]:
    """Return the user's region preference, defaulting to every region."""
    row = session.get(UserPreference, user_id)
    if row is None:
        return clean_user_regions(None)
    return clean_user_regions(row.regions)


def get_user_signal_settings(session: Session, user_id: str) -> Dict:
    """Return the user's signal settings, defaulting when no valid value exists."""
    row = session.get(UserPreference, user_id)
    if row is None or row.signal_settings is None:
        return clean_signal_settings(None)
    return clean_signal_settings(row.signal_settings)


def get_user_preferences(session: Session, user_id: str) -> Dict:
    """Return both preference blocks and whether the account row exists.

    ``signal_settings`` deliberately stays ``None`` for rows created before the
    field existed.  The portal can then seed that block from its local mirror
    instead of silently replacing the user's browser settings with defaults.
    """
    row = session.get(UserPreference, user_id)
    if row is None:
        return {
            "regions": clean_user_regions(None),
            "signal_settings": None,
            "initialized": False,
        }
    return {
        "regions": clean_user_regions(row.regions),
        "signal_settings": (
            clean_signal_settings(row.signal_settings)
            if row.signal_settings is not None
            else None
        ),
        "initialized": True,
    }


def set_user_regions(session: Session, user_id: str, regions: Sequence[str]) -> List[str]:
    """Persist a validated region preference and return it in canonical order."""
    normalized = normalize_user_regions(list(regions))
    row = session.get(UserPreference, user_id)
    now = datetime.now(timezone.utc)
    if row is None:
        session.add(UserPreference(user_id=user_id, regions=normalized, updated_at=now))
    else:
        row.regions = normalized
        row.updated_at = now
    return normalized


def set_user_signal_settings(session: Session, user_id: str, signal_settings: Dict) -> Dict:
    """Persist validated signal settings and return their canonical form."""
    normalized = normalize_signal_settings(signal_settings)
    row = session.get(UserPreference, user_id)
    now = datetime.now(timezone.utc)
    if row is None:
        session.add(
            UserPreference(
                user_id=user_id,
                regions=list(DEFAULT_USER_REGIONS),
                signal_settings=normalized,
                updated_at=now,
            )
        )
    else:
        row.signal_settings = normalized
        row.updated_at = now
    return normalized


# --------------------------------------------------------------------------- watchlist


def list_watchlist(session: Session, owner: str) -> List[Watchlist]:
    return list(
        session.scalars(
            select(Watchlist).where(Watchlist.owner == owner).order_by(Watchlist.created_at.asc())
        )
    )


def reassign_watchlist_owners(session: Session, new_owner: str) -> int:
    """Merge every existing watchlist into one instance-scoped owner."""
    rows = list(
        session.scalars(
            select(Watchlist)
            .where(Watchlist.owner != new_owner)
            .order_by(Watchlist.created_at.asc(), Watchlist.id.asc())
        )
    )
    moved = 0
    for row in rows:
        existing = session.scalar(
            select(Watchlist).where(
                Watchlist.owner == new_owner,
                Watchlist.asset == row.asset,
            )
        )
        if existing is None:
            row.owner = new_owner
        else:
            existing.aliases = normalize_watchlist_aliases(
                list(existing.aliases or []) + list(row.aliases or []),
                existing.asset,
            )
            session.delete(row)
        moved += 1
    return moved


def replace_watchlist(session: Session, owner: str, items: Sequence[Dict]) -> List[Watchlist]:
    session.execute(delete(Watchlist).where(Watchlist.owner == owner))
    now = datetime.now(timezone.utc)
    rows = []
    for item in items:
        asset = str(item.get("asset") or "").strip().upper()
        if not asset:
            continue
        rows.append(
            Watchlist(
                id=new_id(),
                owner=owner,
                asset=asset,
                asset_class=str(item.get("asset_class") or "stock"),
                aliases=normalize_watchlist_aliases(item.get("aliases"), asset),
                created_at=now,
            )
        )
        session.add(rows[-1])
    return rows


def upsert_watchlist_asset(
    session: Session,
    owner: str,
    asset: str,
    asset_class: str,
    aliases: Optional[Sequence[str]] = None,
) -> Watchlist:
    """Insert or refresh one recognised asset, keeping the list unique per owner.

    Re-adding an asset already on the list updates its class and aliases instead
    of violating ``uq_watchlist_owner_asset``.
    """
    symbol = str(asset or "").strip().upper()
    row = session.scalar(
        select(Watchlist).where(Watchlist.owner == owner, Watchlist.asset == symbol)
    )
    cleaned = normalize_watchlist_aliases(list(aliases or []), symbol)
    if row is None:
        row = Watchlist(
            id=new_id(),
            owner=owner,
            asset=symbol,
            asset_class=str(asset_class or "stock"),
            aliases=cleaned,
            created_at=datetime.now(timezone.utc),
        )
        session.add(row)
    else:
        row.asset_class = str(asset_class or row.asset_class)
        if cleaned:
            row.aliases = cleaned
    session.flush()
    return row


def watchlist_assets(session: Session, owner: str) -> List[str]:
    return [row.asset for row in session.scalars(select(Watchlist).where(Watchlist.owner == owner))]


# --------------------------------------------------------------------------- misc


def get_github_cache(session: Session) -> Optional[GithubCache]:
    return session.get(GithubCache, "latest")


def set_github_cache(session: Session, payload: List[Dict]) -> None:
    row = session.get(GithubCache, "latest")
    now = datetime.now(timezone.utc)
    if row is None:
        session.add(GithubCache(id="latest", payload=payload, fetched_at=now))
    else:
        row.payload = payload
        row.fetched_at = now


def get_github_translation(session: Session, url: str, locale: str) -> Optional[GithubTranslation]:
    return session.scalar(
        select(GithubTranslation).where(
            GithubTranslation.url == url,
            GithubTranslation.locale == locale,
        )
    )


def github_translations_for(
    session: Session, urls: Sequence[str], locale: str
) -> Dict[str, GithubTranslation]:
    """Completed repo display-name/description translations keyed by URL."""
    urls = [value for value in urls if value]
    if not urls or not locale:
        return {}
    rows = session.scalars(
        select(GithubTranslation).where(
            GithubTranslation.url.in_(urls),
            GithubTranslation.locale == locale,
            GithubTranslation.status == TRANSLATION_COMPLETED,
        )
    )
    return {row.url: row for row in rows}


def queue_github_translation(
    session: Session,
    url: str,
    locale: str,
    description: str = "",
    full_name: str = "",
    name: str = "",
    status: str = TRANSLATION_PENDING,
) -> GithubTranslation:
    """Create or reset the target-locale row for a cached repo.

    A completed row is never downgraded: a repo that trends again with a
    slightly different description keeps the translation it already paid for.
    """
    row = get_github_translation(session, url, locale)
    if row is not None and row.status == TRANSLATION_COMPLETED:
        return row
    if row is None:
        row = GithubTranslation(url=url, locale=locale, created_at=utcnow())
        session.add(row)
    row.full_name = full_name
    row.name = name
    row.description = description
    row.status = status
    row.retry_count = 0
    row.last_error = ""
    row.updated_at = utcnow()
    return row


def pending_github_translations(
    session: Session, locales: Sequence[str], max_retry: int, limit: int = 50
) -> List[GithubTranslation]:
    """Retryable repo-description work, oldest first (mirrors pending_translations)."""
    locales = [loc for loc in locales if loc]
    if not locales:
        return []
    return list(
        session.scalars(
            select(GithubTranslation)
            .where(
                GithubTranslation.locale.in_(locales),
                GithubTranslation.status.in_(
                    [TRANSLATION_PENDING, TRANSLATION_FAILED]
                ),
                GithubTranslation.retry_count <= max_retry,
            )
            .order_by(GithubTranslation.updated_at.asc())
            .limit(limit)
        )
    )


def mark_github_translation_failed(session: Session, row: GithubTranslation, error: str) -> None:
    row.status = TRANSLATION_FAILED
    row.retry_count = (row.retry_count or 0) + 1
    row.last_error = error[:2000]
    row.updated_at = utcnow()


def mark_github_translation_completed(
    session: Session, row: GithubTranslation, description: str, name: str = ""
) -> None:
    row.name = name
    row.description = description
    row.status = TRANSLATION_COMPLETED
    row.retry_count = 0
    row.last_error = ""
    row.updated_at = utcnow()


#: How long a pushed repo stays suppressed from later digests.
GITHUB_PUSH_WINDOW_DAYS = 30


def _github_full_name(item: Dict) -> str:
    return str(item.get("full_name") or item.get("title") or "").strip()


def record_github_pushes(session: Session, items: List[Dict], *, pushed_at: Optional[datetime] = None) -> int:
    """Upsert the repos a pushed digest actually contained.

    ``pushed_at`` defaults to now; re-pushing the same repo inside a multi
    locale run refreshes the stamp, which keeps the window sliding correctly.
    """
    stamp = pushed_at or datetime.now(timezone.utc)
    recorded = 0
    for item in items:
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        row = session.get(GithubPush, url)
        full_name = _github_full_name(item)
        if row is None:
            session.add(GithubPush(url=url, full_name=full_name, pushed_at=stamp))
        else:
            row.full_name = full_name
            row.pushed_at = stamp
        recorded += 1
    return recorded


def pushed_github_urls(
    session: Session, *, window_days: int = GITHUB_PUSH_WINDOW_DAYS
) -> set:
    """URLs pushed within the sliding window — these are filtered from digests."""
    since = datetime.now(timezone.utc) - timedelta(days=max(0, window_days))
    return set(session.scalars(select(GithubPush.url).where(GithubPush.pushed_at >= since)))


def record_signal_pushes(
    session: Session,
    items: List[Dict],
    *,
    section: str = "",
    locale: str = "",
    pushed_at: Optional[datetime] = None,
) -> int:
    """Upsert the signals a pushed digest actually contained.

    ``pushed_at`` defaults to now; re-pushing inside a multi-locale run
    refreshes the stamp, which keeps the suppression window sliding.
    """
    stamp = pushed_at or datetime.now(timezone.utc)
    recorded = 0
    for item in items:
        signal_id = str(item.get("id") or "").strip()
        if not signal_id:
            continue
        row = session.get(SignalPush, signal_id)
        url_hash_value = str(item.get("url_hash") or "").strip()
        title = str(item.get("title") or "").strip()
        if row is None:
            session.add(
                SignalPush(
                    signal_id=signal_id,
                    url_hash=url_hash_value,
                    title=title,
                    section=section,
                    locale=locale,
                    pushed_at=stamp,
                )
            )
        else:
            row.url_hash = url_hash_value or row.url_hash
            row.title = title or row.title
            row.section = section or row.section
            row.locale = locale or row.locale
            row.pushed_at = stamp
        recorded += 1
    return recorded


def pushed_signal_ids(session: Session) -> set:
    """Ids of every signal that was already pushed at least once.

    The suppression window is deliberately unbounded: a signal that has been
    pushed must never be pushed again, because the pool keeps it visible for
    days while the reader has already seen it.
    """
    return set(session.scalars(select(SignalPush.signal_id)))


def get_setting(session: Session, key: str, default: str = "") -> str:
    row = session.get(LocalSetting, key)
    return row.value if row else default


def set_setting(session: Session, key: str, value: str) -> None:
    now = datetime.now(timezone.utc)
    row = session.get(LocalSetting, key)
    if row is None:
        session.add(LocalSetting(key=key, value=value, updated_at=now))
    else:
        row.value = value
        row.updated_at = now


def prune_old_signals(session: Session, days: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = session.execute(delete(Signal).where(Signal.first_seen_at < cutoff))
    return result.rowcount or 0



# --------------------------------------------------------------------------- yield pool


def replace_yield_pools(
    session: Session,
    items: List[Dict[str, Any]],
    *,
    reporter: str,
    recorded_at: Optional[datetime] = None,
) -> tuple[int, int]:
    """Replace the whole yield snapshot with ``items``.

    A report is a complete crawl, not an incremental delta: rows absent from
    the new snapshot must disappear. The caller validates the entire batch
    before calling this function, so a malformed report never reaches the
    delete. Returns ``(deleted, inserted)``.
    """
    now = recorded_at or utcnow()
    deleted = int(session.execute(delete(YieldPool)).rowcount or 0)
    for item in items:
        session.add(
            YieldPool(
                id=new_id(),
                pool_name=item["pool_name"],
                url=item["url"],
                reference=item["reference"],
                protocol=item["protocol"],
                chain=item["chain"],
                tvl_usd=item["tvl_usd"],
                apy_30d=item["apy_30d"],
                apy_today=item["apy_today"],
                expiry_date=item.get("expiry_date"),
                reporter=reporter,
                recorded_at=now,
            )
        )
    return deleted, len(items)


def list_yield_pools(session: Session) -> List[YieldPool]:
    """Return the current yield snapshot in portal display order."""
    return list(
        session.scalars(
            select(YieldPool).order_by(
                YieldPool.apy_today.desc(),
                YieldPool.apy_30d.desc(),
                YieldPool.tvl_usd.desc(),
                YieldPool.pool_name.asc(),
            )
        )
    )


def delete_yield_pools(
    session: Session,
    *,
    protocol: str,
    chain: str,
) -> int:
    """Delete one protocol/chain slice from the current yield snapshot."""
    result = session.execute(
        delete(YieldPool).where(
            YieldPool.protocol == protocol,
            YieldPool.chain == chain,
        )
    )
    return int(result.rowcount or 0)


def count_rows(session: Session, model) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)
