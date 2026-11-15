"""Shared tokenizer for title and body pre-tokenisation.

Tokenisation happens once, before insert. Queries then work on plain text
columns, which keeps the read path free of jieba and makes the stored data
identical on master and slaver.
"""
from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Iterable, List, Optional

import jieba

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9]+", re.I)

# Small built-in stopword set; the noise words ("的", "and") would otherwise fill
# the index and dilute every score.
STOPWORDS = {
    # English
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from", "has",
    "have", "in", "is", "it", "its", "of", "on", "or", "that", "the", "this",
    "to", "was", "were", "will", "with", "you", "your",
    # Chinese
    "的", "了", "和", "是", "在", "有", "就", "也", "都", "而", "及", "与", "或",
    "一个", "我们", "他们", "这个", "那个", "因为", "所以", "但是", "可以",
}

_lock = threading.Lock()
_user_dict_loaded = False


def configure(user_dict_path: Optional[str], extra_stopwords: Iterable[str]) -> None:
    load_user_dict(user_dict_path)
    add_stopwords(extra_stopwords)


def load_user_dict(user_dict_path: Optional[str]) -> None:
    """Load custom vocabulary once per process."""
    global _user_dict_loaded
    if _user_dict_loaded or not user_dict_path:
        return
    path = Path(user_dict_path)
    if not path.exists():
        logger.warning("tokenizer user dict not found: %s", path)
        _user_dict_loaded = True
        return
    with _lock:
        jieba.load_userdict(str(path))
    logger.info("tokenizer user dict loaded: %s", path)
    _user_dict_loaded = True


def add_stopwords(words: Iterable[str]) -> None:
    STOPWORDS.update({w.strip().lower() for w in words if w and w.strip()})


def tokenize(text: str) -> List[str]:
    """Split mixed Chinese/English text into searchable tokens."""
    if not text:
        return []

    tokens: List[str] = []
    for raw in jieba.cut(text.strip(), cut_all=False, HMM=True):
        token = raw.strip().lower()
        if not token or token in STOPWORDS:
            continue
        if _TOKEN_RE.fullmatch(token):
            tokens.append(token)
        elif not token.isascii():
            tokens.append(token)
    return tokens


def build_search_text(
    text: str,
    assets: Optional[List[str]] = None,
    max_chars: int = 20000,
) -> str:
    """Pre-tokenise ``text`` and append tickers verbatim.

    Assets are appended in upper case so an asset lookup is not defeated by the
    lower-casing applied to ordinary tokens.
    """
    body = (text or "")[:max_chars]
    tokens = tokenize(body)
    for asset in assets or []:
        if asset:
            tokens.append(asset.upper())
    return " ".join(tokens)


_PUNCT_RE = re.compile(r"[\W_]+", re.UNICODE)
_DIGIT_RE = re.compile(r"[0-9]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def dedup_query_tokens(title: str, content: str, max_tokens: int) -> List[str]:
    """Build the dedup query from ``title`` + ``content``.

    Keeps only standard Chinese words and pure English words (>=2 letters):
    punctuation, digits, single letters, HTML tags and URLs are stripped
    before tokenising, and repeated tokens collapse to one so a repeated word
    cannot multiply the BM25 contribution. Order-preserving, capped at
    ``max_tokens`` (<=0 means uncapped).
    """
    body = re.sub(r"<[^>]+>", " ", f"{title or ''} {content or ''}")
    body = re.sub(r"https?://\S+", " ", body)
    out: List[str] = []
    seen: set = set()
    for raw in jieba.cut(body.strip(), cut_all=False, HMM=True):
        token = raw.strip().lower()
        if not token or token in STOPWORDS or token in seen:
            continue
        if _PUNCT_RE.fullmatch(token) or _DIGIT_RE.search(token):
            continue
        if token.isascii():
            if not token.isalpha() or len(token) < 2:
                continue
        elif not _CJK_RE.search(token):
            continue
        seen.add(token)
        out.append(token)
        if 0 < max_tokens <= len(out):
            break
    return out


def build_keywords(text: str, limit: int = 10) -> List[str]:
    """Fixed-size keyword set used for signal-to-signal lookups."""
    tokens = tokenize(text)
    seen = set()
    keywords: List[str] = []
    for token in tokens:
        if len(token) < 2 or token in seen:
            continue
        seen.add(token)
        keywords.append(token)
        if len(keywords) >= limit:
            break
    return keywords


def normalize_keywords(value: object, limit: int = 10) -> List[str]:
    """Coerce an LLM-supplied keyword list into the stored shape.

    The model is asked for exactly 10 keywords, but nothing downstream may
    assume it complied: a bare string would otherwise survive ``list(...)`` as
    one keyword per character, and a 40-item dump would silently bloat the row.
    Returns ``[]`` for anything that is not a usable list, letting the caller
    fall back to :func:`build_keywords`.
    """
    if not isinstance(value, (list, tuple)):
        return []
    keywords: List[str] = []
    seen = set()
    for item in value:
        if not isinstance(item, str):
            continue
        token = item.strip().lower()
        if not token or token in seen:
            continue
        seen.add(token)
        keywords.append(token)
        if len(keywords) >= limit:
            break
    return keywords
