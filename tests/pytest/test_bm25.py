"""BM25 unit tests (§5.5).

Two properties matter beyond "it returns a number":

- Title and body are scored with independent parameter sets, so a long body
  must not drag the title score down.
- Corpus statistics come from the collection, not from the recalled subset.
  Ranking a small subset against itself collapses idf, which would make the
  configured thresholds unreachable on a real instance.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.dedup.bm25 import (
    BM25Params,
    CorpusStats,
    Document,
    build_stats,
    rank,
    score_document,
)


def doc(doc_id: str, title: str, content: str, source: str = "") -> Document:
    return Document(id=doc_id, title_search=title, content_search=content, source_name=source)


# --------------------------------------------------------------------------- parameters


def test_title_and_body_use_independent_parameters():
    long_body = " ".join(["filler"] * 200 + ["bitcoin"])
    candidate = doc("d", "bitcoin", long_body)
    # A second, short document so the corpus averages differ from this one and
    # length normalisation is actually exercised rather than being a no-op.
    corpus = [candidate, doc("other", "unrelated headline", "short body")]

    uniform = score_document(candidate, ["bitcoin"], build_stats(corpus))
    # Body is longer than the corpus average, so dropping its normalisation
    # lifts the score.
    lenient_body = score_document(
        candidate, ["bitcoin"], build_stats(corpus), params=BM25Params(b_content=0.0)
    )
    assert lenient_body > uniform

    # Title is shorter than the corpus average, so the same knob moves it the
    # other way — proof the two fields are normalised separately.
    lenient_title = score_document(
        candidate, ["bitcoin"], build_stats(corpus), params=BM25Params(b_title=0.0)
    )
    assert lenient_title < uniform


def test_title_weight_lifts_a_title_hit():
    stats = CorpusStats({"copper": 1}, total_docs=2, total_title_len=1, total_content_len=1)
    only_body = doc("d", "unrelated headline", "copper")
    only_title = doc("d", "copper", "unrelated body")

    assert score_document(only_title, ["copper"], stats, title_weight=3.0) > score_document(
        only_body, ["copper"], stats, title_weight=3.0
    )


# --------------------------------------------------------------------------- corpus statistics


def test_statistics_come_from_corpus_not_from_ranked_subset():
    # "copper" sits in every document of the window, so scoring the recalled row
    # against the window must discount it. Ranking that row in isolation makes
    # df == N == 1 and hands it the maximum idf, which would push ordinary
    # matches over any configured threshold.
    corpus = [doc(f"d{i}", "copper smelter update", "copper market commentary") for i in range(9)]
    corpus.append(doc("target", "copper", "copper"))
    query = ["copper"]

    with_corpus = rank([corpus[-1]], query, corpus=corpus)[0][1]
    without_corpus = rank([corpus[-1]], query)[0][1]
    assert with_corpus < without_corpus


