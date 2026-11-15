"""Application-layer BM25.

Implemented here rather than delegated to PostgreSQL ``ts_rank_cd`` or SQLite
FTS5 ``bm25()``: those two rank on different scales, so a single threshold
would behave differently on master and slaver. Databases only recall
candidates; the score below is computed the same way on both sides.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

DEFAULT_K1 = 1.5
DEFAULT_B = 0.75
DEFAULT_TITLE_WEIGHT = 3.0


@dataclass
class BM25Params:
    """Two independent parameter sets, one per field (§5.5).

    Title and body have very different lengths, so sharing one length
    normalisation between them skews whichever field is longer.
    """

    k1_title: float = DEFAULT_K1
    b_title: float = DEFAULT_B
    k1_content: float = DEFAULT_K1
    b_content: float = DEFAULT_B


@dataclass
class Document:
    id: str
    title_search: str
    content_search: str
    source_name: str = ""
    meta: dict = field(default_factory=dict)


def _tokenize_stored(value: str) -> List[str]:
    return [token for token in (value or "").split() if token]


class CorpusStats:
    """Corpus level term statistics, bound once per scoring run."""

    def __init__(
        self,
        doc_frequencies: Dict[str, int],
        total_docs: int,
        total_title_len: int = 0,
        total_content_len: int = 0,
    ):
        self.doc_frequencies = doc_frequencies
        self.total_docs = total_docs
        self.total_title_len = total_title_len
        self.total_content_len = total_content_len

    def idf(self, term: str) -> float:
        df = self.doc_frequencies.get(term, 0)
        # Robertson/Sparck-Jones idf with the +1 guard that keeps it positive.
        return math.log(1.0 + (self.total_docs - df + 0.5) / (df + 0.5))

    @property
    def avg_title_len(self) -> float:
        return max(1.0, self.total_title_len / max(self.total_docs, 1))

    @property
    def avg_content_len(self) -> float:
        return max(1.0, self.total_content_len / max(self.total_docs, 1))


def score_document(
    doc: Document,
    query_tokens: Sequence[str],
    stats: CorpusStats,
    title_weight: float = DEFAULT_TITLE_WEIGHT,
    params: Optional[BM25Params] = None,
) -> float:
    """Weighted title + body BM25 for one document."""
    params = params or BM25Params()
    if not query_tokens:
        return 0.0

    title_tokens = _tokenize_stored(doc.title_search)
    content_tokens = _tokenize_stored(doc.content_search)
    if not title_tokens and not content_tokens:
        return 0.0

    score = 0.0
    if title_tokens:
        score += (
            _score_field(
                query_tokens, title_tokens, stats.avg_title_len, stats, params.k1_title, params.b_title
            )
            * title_weight
        )
    if content_tokens:
        score += _score_field(
            query_tokens,
            content_tokens,
            stats.avg_content_len,
            stats,
            params.k1_content,
            params.b_content,
        )
    return score


def _score_field(
    query_tokens: Sequence[str],
    doc_tokens: List[str],
    avg_len: float,
    stats: CorpusStats,
    k1: float,
    b: float,
) -> float:
    doc_counts = Counter(doc_tokens)
    doc_len = len(doc_tokens)
    score = 0.0
    for token in query_tokens:
        freq = doc_counts.get(token)
        if not freq:
            continue
        denom = freq + k1 * (1 - b + b * (doc_len / avg_len if avg_len else 1.0))
        score += stats.idf(token) * (freq * (k1 + 1)) / denom
    return score


def build_stats(documents: Iterable[Document]) -> CorpusStats:
    """Compute idf plus the two average field lengths in one pass."""
    doc_frequencies: Dict[str, int] = {}
    total = 0
    total_title = 0
    total_content = 0
    for doc in documents:
        total += 1
        title_tokens = _tokenize_stored(doc.title_search)
        content_tokens = _tokenize_stored(doc.content_search)
        total_title += len(title_tokens)
        total_content += len(content_tokens)
        for token in set(title_tokens) | set(content_tokens):
            doc_frequencies[token] = doc_frequencies.get(token, 0) + 1

    return CorpusStats(
        doc_frequencies, max(total, 1), total_title_len=total_title, total_content_len=total_content
    )


def rank(
    documents: Sequence[Document],
    query_tokens: Sequence[str],
    title_weight: float = DEFAULT_TITLE_WEIGHT,
    params: Optional[BM25Params] = None,
    same_source: str = "",
    same_source_bonus: float = 0.0,
    top_k: int = 10,
    corpus: Optional[Sequence[Document]] = None,
) -> List[tuple]:
    """Return ``(doc, score)`` pairs sorted by descending score.

    ``corpus`` supplies the corpus statistics. It defaults to ``documents``,
    which is only correct when the caller ranked the whole collection: recall
    returns a handful of rows, and idf computed over a handful of rows collapses
    towards zero, which would make any configured threshold unreachable. Callers
    that score a recalled subset must pass the window corpus explicitly.
    """
    if not query_tokens or not documents:
        return []

    stats = build_stats(corpus if corpus is not None else documents)
    scored = []
    for doc in documents:
        value = score_document(doc, query_tokens, stats, title_weight, params)
        if value <= 0:
            continue
        if same_source and doc.source_name and doc.source_name == same_source:
            value += same_source_bonus
        scored.append((doc, value))

    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:top_k]