"""Two-level dedup: URL → BM25 (§5.4).

Fully automatic: there is no manual arbitration step or pending queue. URL hits
and BM25 high-threshold hits are archived and logged for threshold tuning.
Candidates in the BM25 grey band continue as unique candidates.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from src.config import get_dedup_config, get_ranking_config
from src.db.models import Signal
from src.db.repositories import (
    get_signal_by_url,
    merge_source_url,
    recall_candidates,
    record_duplicate,
    window_documents,
)
from src.db.session import refresh_sqlite_fts
from src.dedup.bm25 import BM25Params, Document, rank
from src.dedup.tokenizer import dedup_query_tokens
from src.dedup.urls import canonical_url, is_valid_url, url_hash

logger = logging.getLogger(__name__)

MATCH_URL = "url"
MATCH_BM25 = "bm25"

DECIDED_RULE = "rule"


@dataclass
class DedupDecision:
    """Outcome for one candidate."""

    is_duplicate: bool
    match_level: str = ""
    decided_by: str = ""
    matched_signal: Optional[Signal] = None
    bm25_score: float = 0.0
    confidence: float = 0.0
    reason: str = ""


def _to_document(signal: Signal) -> Document:
    return Document(
        id=signal.id,
        title_search=signal.title_search or "",
        content_search=signal.content_search or "",
        source_name=signal.source_name or "",
        meta={"url": signal.url},
    )


def _bm25_params(config: Dict) -> BM25Params:
    ranking = get_ranking_config(config)
    return BM25Params(
        k1_title=ranking["k1_title"],
        b_title=ranking["b_title"],
        k1_content=ranking["k1_content"],
        b_content=ranking["b_content"],
    )


def check_candidate(
    session: Session,
    config: Dict,
    candidate: Dict,
    corpus_limit: int = 2000,
) -> DedupDecision:
    """Decide whether ``candidate`` duplicates an existing signal.

    ``candidate`` must carry ``title``, ``content`` and the pre-tokenised
    ``title_search`` / ``content_search`` columns. URL matching is skipped when
    ``canonical_url`` is empty, leaving BM25 as the only deduplication stage.
    """
    canonical = candidate["canonical_url"]
    if canonical:
        existing = get_signal_by_url(session, canonical, candidate["url_hash"])
        if existing is not None:
            return DedupDecision(
                is_duplicate=True,
                match_level=MATCH_URL,
                decided_by=DECIDED_RULE,
                matched_signal=existing,
                confidence=1.0,
                reason="canonical url already stored",
            )

    dedup = get_dedup_config(config)
    ranking = get_ranking_config(config)
    since = datetime.now(timezone.utc) - timedelta(hours=dedup["window_hours"])

    # Query head: deduped clean words only (title first, then body). Scoring a
    # whole article, with punctuation/HTML noise and repeated tokens, inflates
    # every score by orders of magnitude and a static threshold stops
    # separating reprints from unrelated news.
    query_tokens = dedup_query_tokens(
        candidate.get("title", ""),
        candidate.get("content", ""),
        dedup["max_query_tokens"],
    )
    if not query_tokens:
        return DedupDecision(is_duplicate=False)
    recalled = recall_candidates(session, query_tokens, since, limit=200)
    if not recalled:
        return DedupDecision(is_duplicate=False)

    # idf must come from the whole window, not from the recalled handful: a term
    # missing from every other document carries no information, and scoring a
    # subset against its own statistics would push every score towards zero.
    corpus = [_to_document(row) for row in window_documents(session, since, limit=corpus_limit)]

    ranked: List[Tuple[Document, float]] = rank(
        [_to_document(row) for row in recalled],
        query_tokens,
        title_weight=ranking["title_weight"],
        params=_bm25_params(config),
        same_source=candidate.get("source_name", ""),
        same_source_bonus=dedup["same_source_bonus"],
        top_k=dedup["top_k"],
        corpus=corpus,
    )
    if not ranked:
        return DedupDecision(is_duplicate=False)

    top_doc, top_score = ranked[0]
    by_id = {row.id: row for row in recalled}
    top_signal = by_id[top_doc.id]

    if top_score < dedup["bm25_low_threshold"]:
        return DedupDecision(is_duplicate=False, bm25_score=top_score)
    if top_score >= dedup["bm25_high_threshold"]:
        return DedupDecision(
            is_duplicate=True,
            match_level=MATCH_BM25,
            decided_by=DECIDED_RULE,
            matched_signal=top_signal,
            bm25_score=top_score,
            confidence=1.0,
            reason=f"bm25 {top_score:.2f} >= high threshold {dedup['bm25_high_threshold']}",
        )

    return DedupDecision(
        is_duplicate=False,
        bm25_score=top_score,
        reason=(
            f"bm25 {top_score:.2f} is below high threshold "
            f"{dedup['bm25_high_threshold']}"
        ),
    )


async def check(
    session: Session,
    config: Dict,
    candidate: Dict,
    corpus_limit: int = 2000,
) -> DedupDecision:
    return check_candidate(session, config, candidate, corpus_limit)


def apply_url_duplicate(session: Session, candidate: Dict) -> bool:
    """Cheap URL-only dedup used before quota reservation and translation.

    Master reports may still need a translation call, and a reporter's daily
    scoring budget must not be consumed by a URL that is already stored.  This
    helper therefore runs the deterministic URL gate alone; the full URL+BM25
    pass still runs later on the translated text.
    """
    canonical = candidate.get("canonical_url") or ""
    if not canonical:
        return False
    existing = get_signal_by_url(session, canonical, candidate["url_hash"])
    if existing is None:
        return False
    merge_source_url(session, existing, canonical)
    refresh_sqlite_fts(session, [existing.url_hash])
    record_duplicate(
        session,
        candidate_url=canonical,
        candidate_title=candidate.get("title", ""),
        matched_signal_id=existing.id,
        match_level=MATCH_URL,
        decided_by=DECIDED_RULE,
        bm25_score=0.0,
        confidence=1.0,
        reason="canonical url already stored",
    )
    logger.info(
        "dedup hit level=url by=rule url=%s -> signal_id=%s",
        canonical,
        existing.id,
    )
    return True


def apply_candidate(
    session: Session,
    config: Dict,
    candidate: Dict,
) -> Tuple[bool, DedupDecision]:
    """Run dedup and archive any duplicate.

    Returns ``(is_duplicate, decision)``. The archive row and the application
    log are written for every duplicate; repeating the same hit is idempotent.
    Only URL-level hits merge the candidate URL into the survivor's source
    list; a BM25 hit is probabilistic and must never bury the candidate.
    """
    decision = check_candidate(session, config, candidate)
    if not decision.is_duplicate or decision.matched_signal is None:
        return False, decision

    canonical = candidate["canonical_url"]
    # Only deterministic URL-level hits merge the candidate URL into the
    # survivor's source list. A BM25 hit is probabilistic: merging on it would
    # permanently bury a false-kill article behind the survivor's URL scan.
    if canonical and decision.match_level == MATCH_URL:
        merge_source_url(session, decision.matched_signal, canonical)
        refresh_sqlite_fts(session, [decision.matched_signal.url_hash])

    record_duplicate(
        session,
        candidate_url=canonical,
        candidate_title=candidate.get("title", ""),
        matched_signal_id=decision.matched_signal.id,
        match_level=decision.match_level,
        decided_by=decision.decided_by,
        bm25_score=decision.bm25_score,
        confidence=decision.confidence,
        reason=decision.reason,
    )
    logger.info(
        "dedup hit level=%s by=%s url=%s -> signal_id=%s bm25=%.2f",
        decision.match_level,
        decision.decided_by,
        canonical or "<no-url>",
        decision.matched_signal.id,
        decision.bm25_score,
    )
    return True, decision


async def apply(
    session: Session,
    config: Dict,
    candidate: Dict,
) -> Tuple[bool, DedupDecision]:
    return apply_candidate(session, config, candidate)


def prepare_candidate(item: Dict) -> Optional[Dict]:
    """Canonicalise an optional URL and pre-tokenise the search columns.

    Tokenisation happens here, before insert (§5.5): the query path never calls
    jieba.

    ``title_search`` and ``content_search`` are the BM25 corpus and nothing else
    may join them. ``summary_search`` / ``keywords_search`` exist only for user
    search, so they are tokenised here too but never reach :func:`_to_document`
    or the SQLite FTS table.
    """
    from src.dedup.tokenizer import build_search_text

    raw_value = item.get("url") if "url" in item else item.get("link")
    raw_url = str(raw_value or "").strip()
    if raw_url and not is_valid_url(raw_url):
        return None

    canonical = canonical_url(raw_url) if raw_url else ""

    assets = list(item.get("assets") or [])
    title = item.get("title") or ""
    content = item.get("content") or ""
    summary = item.get("summary") or ""
    keywords = list(item.get("keywords") or [])

    return {
        **item,
        "canonical_url": canonical,
        "url_hash": url_hash(canonical) if canonical else hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        "ignore_bm25_below_low": bool(item.get("ignore_bm25_below_low")),
        "title_search": build_search_text(title, assets),
        "content_search": build_search_text(content, assets),
        "summary_search": build_search_text(summary, assets),
        "keywords_search": build_search_text(" ".join(keywords), assets),
    }
