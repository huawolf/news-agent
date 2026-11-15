"""Target-locale translation for the master's shared pool.

The main ``signal`` table stores one locale — the instance's ``output_language``.
This module fills the other locales into ``signal_translation`` and is the only
writer of that table.

Two rules shape the design:

- ``content`` is never translated. It is the source-language body and stays as
  it is in every locale.
- A translation failure never touches the signal. The row records the error and
  the portal serves the main-table text with ``fallback: true``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from src.config import get_content_languages, get_output_language, get_translation_config
from src.db.models import GithubTranslation, Signal, SignalTranslation
from src.db.repositories import (
    TRANSLATION_COMPLETED,
    TRANSLATION_PENDING,
    mark_github_translation_completed,
    mark_github_translation_failed,
    mark_translation_completed,
    mark_translation_failed,
    get_github_translation,
    pending_github_translations,
    pending_translations,
    queue_github_translation,
    queue_translation,
)
from src.llm import call_llm, load_prompt
from src.markdown_utils import normalize_str_list

logger = logging.getLogger(__name__)

LOCALE_NAMES = {"zh": "简体中文", "en": "English"}

DEFAULT_PROMPT = "prompts/translate_signal.md"

# A translation only needs the three display fields; anything longer is context
# the model does not use and would only cost tokens.
MAX_ITEM_CHARS = 1200


def queue_missing_translations(
    session: Session, signal_ids: Sequence[str], config: Dict
) -> int:
    """Create a pending row per target locale for freshly inserted signals.

    Nothing is queued when the instance is single-language or is not a master,
    which keeps a slaver from ever paying for a locale it does not serve.
    """
    settings = get_translation_config(config)
    if not settings["enabled"]:
        return 0

    queued = 0
    for signal_id in signal_ids:
        for locale in settings["targets"]:
            queue_translation(session, signal_id, locale, status=TRANSLATION_PENDING)
            queued += 1
    return queued


def queue_missing_github_translations(session: Session, repos: Sequence[Dict], config: Dict) -> int:
    """Queue the trending snapshot's display text for every target locale.

    Called after the fetch cycle replaces the ``github_cache`` payload, so the
    queue always mirrors what the portal is about to serve. The scorer authors
    the snapshot's display text in the main locale (the LLM summary, with the
    raw English description as fallback), so the targets follow the same rule
    as signals: served locales minus the main one. A repo without display text
    has nothing to translate; ``full_name`` stays the raw identifier and only
    ``name`` / ``description`` are stored.
    """
    settings = get_translation_config(config)
    if not settings["enabled"]:
        return 0

    queued = 0
    for repo in repos:
        url = str(repo.get("url") or "").strip()
        display = str(repo.get("summary") or repo.get("description") or "").strip()
        if not url or not display:
            continue
        for locale in settings["targets"]:
            existing = get_github_translation(session, url, locale)
            if existing is not None and existing.status == TRANSLATION_COMPLETED:
                continue
            queue_github_translation(
                session,
                url,
                locale,
                description=display,
                full_name=str(repo.get("full_name") or "").strip(),
                status=TRANSLATION_PENDING,
            )
            queued += 1
    return queued


def store_reported_translation(
    session: Session, signal_id: str, locale: str, prepared: Dict
) -> None:
    """Keep a slaver-reported locale as-is instead of re-deriving it.

    When a slaver reports Chinese and the master stores English, the Chinese it
    already paid for is the target-locale text. Re-translating it back would
    cost a second call and produce a worse result than the original.
    """
    queue_translation(
        session,
        signal_id,
        locale,
        title=prepared.get("title") or "",
        summary=prepared.get("summary") or "",
        keywords=list(prepared.get("keywords") or []),
        status=TRANSLATION_COMPLETED,
    )


def _parse_translation_response(response: str) -> List[Dict]:
    text = (response or "").strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
        for pattern in (r"\{.*\}", r"\[.*\]"):
            match = re.search(pattern, text, re.DOTALL)
            if match:
                try:
                    parsed = json.loads(match.group())
                    break
                except json.JSONDecodeError:
                    continue
    if parsed is None:
        raise ValueError(f"无法从翻译响应中解析 JSON: {text[:200]}...")

    if isinstance(parsed, dict):
        for key in ("items", "results", "translations", "data"):
            if isinstance(parsed.get(key), list):
                parsed = parsed[key]
                break
        else:
            list_values = [v for v in parsed.values() if isinstance(v, list)]
            if len(list_values) != 1:
                raise ValueError(f"翻译响应中没有 items 数组: {text[:200]}...")
            parsed = list_values[0]
    if not isinstance(parsed, list):
        raise ValueError(f"翻译响应不是数组: {type(parsed)}")
    return parsed


async def translate_items(
    items: List[Dict], locale: str, config: Dict
) -> Dict[str, Dict]:
    """Translate a batch of ``{link, title, summary, keywords}`` items.

    Returns a mapping keyed by ``link``. A link the model dropped is simply
    absent from the result, leaving that signal's row pending for a retry.
    """
    if not items:
        return {}

    llm_config = config.get("llm") or {}
    prompt_path = str(
        (llm_config.get("prompts") or {}).get("translate_signal", DEFAULT_PROMPT)
    )
    payload = [
        {
            "link": item.get("link", ""),
            "title": (item.get("title") or "")[:300],
            "summary": (item.get("summary") or "")[:MAX_ITEM_CHARS],
            "keywords": normalize_str_list(item.get("keywords"))[:10],
        }
        for item in items
    ]
    prompt = load_prompt(
        prompt_path,
        target_language=LOCALE_NAMES.get(locale, locale),
        items_json=json.dumps(payload, ensure_ascii=False, indent=2),
    )
    response = await call_llm(prompt, llm_config, response_format={"type": "json_object"})
    parsed = _parse_translation_response(response)

    results: Dict[str, Dict] = {}
    for row in parsed:
        if not isinstance(row, dict):
            continue
        link = str(row.get("link") or "").strip()
        if not link:
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            # An empty title would replace a readable main-table title with
            # nothing, so treat it as a miss and retry later.
            continue
        results[link] = {
            "title": title,
            "summary": str(row.get("summary") or "").strip(),
            "keywords": normalize_str_list(row.get("keywords"))[:10],
        }
    return results


async def _translate_chunk(
    session: Session,
    chunk: List[Dict],
    rows: List[SignalTranslation],
    locale: str,
    config: Dict,
) -> Tuple[int, int]:
    """One batch: either every row completes or every row records the error."""
    try:
        results = await translate_items(chunk, locale, config)
    except Exception as error:  # noqa: BLE001 - recorded per row, never raised
        logger.warning(
            "translation batch failed locale=%s size=%d error=%s",
            locale,
            len(chunk),
            error,
        )
        for row in rows:
            mark_translation_failed(session, row, str(error))
        return 0, len(rows)

    translated = 0
    failed = 0
    for row in rows:
        result = results.get(
            (row.signal.url or row.signal.id) if row.signal else ""
        )
        if result is None:
            mark_translation_failed(session, row, "翻译响应缺少该条内容")
            failed += 1
            continue
        mark_translation_completed(
            session,
            row,
            result["title"],
            result["summary"],
            result["keywords"],
        )
        translated += 1
    return translated, failed


async def _translate_github_chunk(
    session: Session,
    chunk: List[Dict],
    rows: List[GithubTranslation],
    config: Dict,
) -> Tuple[int, int]:
    """One repo batch: all rows complete or all record the error.

    The LLM returns the localized repo display name as the translated title;
    both ``name`` and ``description`` are stored, while ``full_name`` stays
    the raw identifier.
    """
    try:
        results = await translate_items(chunk, rows[0].locale, config)
    except Exception as error:  # noqa: BLE001 - recorded per row, never raised
        logger.warning(
            "github translation batch failed size=%d error=%s",
            len(chunk),
            error,
        )
        for row in rows:
            mark_github_translation_failed(session, row, str(error))
        return 0, len(rows)

    translated = 0
    failed = 0
    for row in rows:
        result = results.get(row.url)
        if result is None or not result["summary"]:
            mark_github_translation_failed(session, row, "翻译响应缺少该条内容")
            failed += 1
            continue
        mark_github_translation_completed(
            session, row, result["summary"], name=result["title"]
        )
        translated += 1
    return translated, failed


async def run_translation_cycle(
    session: Session, config: Dict, *, limit: int = 50
) -> Dict[str, int]:
    """Drain the translation queue once.

    Returns ``{"translated": n, "failed": n}``. Safe to call on every fetch
    cycle: a failure only increments ``retry_count`` on the affected row.

    Batches are the unit of concurrency, not locales. With a single target
    locale — the normal case, since there are only two locales — grouping by
    locale would make ``concurrency`` a no-op and serialise every call.
    """
    settings = get_translation_config(config)
    if not settings["enabled"]:
        return {"translated": 0, "failed": 0}

    signal_targets: List[SignalTranslation] = pending_translations(
        session, settings["targets"], settings["max_retry"], limit=limit
    )
    # GitHub display text is queued in the same cycle; an empty signal queue
    # must not skip it, so the early exit only fires when both are drained.
    # The snapshot display text is main-locale authored, so GitHub rows drain
    # through the same ``targets`` list as signal translations.
    github_targets: List[GithubTranslation] = pending_github_translations(
        session, settings["targets"], settings["max_retry"], limit=limit
    )
    if not signal_targets and not github_targets:
        return {"translated": 0, "failed": 0}

    per_locale: Dict[str, List[SignalTranslation]] = {}
    for row in signal_targets:
        signal = session.get(Signal, row.signal_id)
        if signal is None:
            # The signal was pruned after the row was queued; drop the
            # orphaned translation rather than translating nothing.
            session.delete(row)
            continue
        row.signal = signal
        per_locale.setdefault(row.locale, []).append(row)

    size = settings["batch_size"]
    chunks: List[Tuple[List[Dict], List[SignalTranslation], str]] = []
    for locale, rows in per_locale.items():
        for start in range(0, len(rows), size):
            window = rows[start : start + size]
            chunks.append(
                (
                    [
                        {
                            "link": row.signal.url or row.signal.id,
                            "title": row.signal.title,
                            "summary": row.signal.summary,
                            "keywords": list(row.signal.keywords or []),
                        }
                        for row in window
                    ],
                    window,
                    locale,
                )
            )

    # GitHub repo descriptions share the same batching machinery; the queue is
    # drained in the same cycle so the portal never serves one language for
    # signals and another for trending repos.
    github_rows_by_locale: Dict[str, List[GithubTranslation]] = {}
    for row in github_targets:
        github_rows_by_locale.setdefault(row.locale, []).append(row)
    github_chunks: List[Tuple[List[Dict], List[GithubTranslation]]] = []
    for rows in github_rows_by_locale.values():
        for start in range(0, len(rows), size):
            window = rows[start : start + size]
            github_chunks.append(
                (
                    [
                        {
                            "link": row.url,
                            "title": row.full_name,
                            "summary": row.description,
                            "keywords": [],
                        }
                        for row in window
                    ],
                    window,
                )
            )

    # One session serves every task: the ORM work is synchronous and never
    # interleaves, because the only await inside a chunk is the LLM call and
    # all row updates happen after it returns.
    semaphore = asyncio.Semaphore(settings["concurrency"])

    async def run_chunk(
        chunk: List[Dict], rows: List[SignalTranslation], locale: str
    ) -> Tuple[int, int]:
        async with semaphore:
            return await _translate_chunk(session, chunk, rows, locale, config)

    async def run_github_chunk(
        chunk: List[Dict], rows: List[GithubTranslation]
    ) -> Tuple[int, int]:
        async with semaphore:
            return await _translate_github_chunk(session, chunk, rows, config)

    results = await asyncio.gather(
        *(run_chunk(*chunk) for chunk in chunks),
        *(run_github_chunk(*chunk) for chunk in github_chunks),
    )
    translated = sum(item[0] for item in results)
    failed = sum(item[1] for item in results)

    session.flush()
    logger.info(
        "translation cycle done targets=%d chunks=%d translated=%d failed=%d",
        len(signal_targets),
        len(chunks),
        translated,
        failed,
    )
    return {"translated": translated, "failed": failed}


def resolve_locale(requested: Optional[str], config: Dict) -> str:
    """Clamp a caller-supplied locale to one this instance can serve.

    The main locale is always servable; a target locale only when a translation
    exists, in which case the portal falls back per row if it is still pending.
    """
    main_locale = get_output_language(config)
    languages = get_content_languages(config)
    locale = (requested or "").strip().lower()
    if locale in languages:
        return locale
    return main_locale


__all__ = [
    "LOCALE_NAMES",
    "queue_missing_translations",
    "resolve_locale",
    "run_translation_cycle",
    "store_reported_translation",
    "translate_items",
]
