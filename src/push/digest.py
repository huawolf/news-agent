"""Digest assembly (§5.3).

A digest has three sections — hot signals, watchlist signals and GitHub
repos. The selection rules live here, in one place, because every rule below
exists to stop the reader from seeing the same thing twice:

1. **Asset dedup across the whole digest.** An asset appears at most once in
   the entire digest: the highest-scoring signal for that asset wins, and
   the asset is spent for good. A digest that pushes five Bitcoin stories is
   five stories about one event, which is exactly the noise the reader opened
   the channel to avoid. The hot section spends first, so the watchlist
   section can only use assets the hot section left free.
2. **Already-pushed suppression.** A signal that was pushed before never goes
   out again. The pool keeps it visible for days, so without this memory the
   morning and evening digests would repeat the same rows.
3. **Cross-section exclusion.** The watchlist section never repeats a signal
   the hot section already carried. This is a consequence of rule 1: the two
   sections share one asset ledger, so a spent asset cannot reappear.

The three rules are applied in that order, so a signal is dropped by the
earliest rule that catches it and the reason is unambiguous.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from src.api.serializers import signal_items
from src.config import get_mode, get_output_language, get_push_limits
from src.db.bootstrap import SLAVER_WATCHLIST_OWNER
from src.db.repositories import (
    get_github_cache,
    list_watchlist,
    pushed_github_urls,
    pushed_signal_ids,
    recent_signals,
    record_github_pushes,
    record_signal_pushes,
    set_setting,
)
from src.translation import resolve_locale

from . import send_to_platforms

logger = logging.getLogger(__name__)


#: One digest per locale, so a Chinese portal reader never sees the English
#: push of the same morning. ``latest_digest`` itself stays as the main-locale
#: key for backward compatibility with existing readers.
DIGEST_KEY = "latest_digest:{locale}"


def digest_key(locale: str) -> str:
    return DIGEST_KEY.format(locale=locale or "en")


def _github_item(repo: dict) -> dict:
    """Map a ``github_cache`` payload entry to the digest item shape.

    The renderer reads title / url / summary. The scorer's summary (main
    locale) is preferred over the raw one-liner description, and the star
    count lives in ``stars_total``.
    """
    full_name = str(repo.get("full_name") or "").strip()
    return {
        "title": full_name or str(repo.get("url") or ""),
        "url": repo.get("url"),
        "summary": str(repo.get("summary") or repo.get("description") or "").strip(),
        "stars": repo.get("stars") or repo.get("stars_total"),
    }


class AssetLedger:
    """Which assets a digest has already spent (§5.3 rule 1).

    One ledger is shared by the hot and the watchlist section, so an asset
    appears at most once in the whole digest: the hot section spends ``BTC``,
    and the watchlist section cannot spend it again. Without a shared ledger
    the reader would meet the same Bitcoin story under two headings.

    Rows arrive in the pool's own order — newest batch first, then
    ``final_score`` descending — so the first row seen for an asset is the
    best one. A signal without assets is a signal about no particular asset
    and is always kept: there is nothing to collide with.
    """

    def __init__(self) -> None:
        self.taken_assets: set[str] = set()
        self.taken_ids: set[str] = set()

    def take(self, row: Any, *, exclude_ids: Iterable[str] = ()) -> bool:
        """Claim ``row`` for the digest. ``False`` means it is a duplicate."""
        if row.id in exclude_ids or row.id in self.taken_ids:
            return False
        assets = [str(asset) for asset in (row.assets or [])]
        if assets:
            if any(asset in self.taken_assets for asset in assets):
                # Any overlap means the reader would see the same asset twice.
                # A multi-asset signal is rejected as a whole; it must not
                # slip through merely because one of its other assets is new.
                return False
            self.taken_assets.update(assets)
        self.taken_ids.add(row.id)
        return True

    def select(
        self,
        rows: Sequence[Any],
        *,
        limit: int,
        exclude_ids: Iterable[str] = (),
        topic_ledger: "TopicLedger | None" = None,
    ) -> list:
        """Claim the best rows for ``limit`` slots, one per asset."""
        excluded = set(exclude_ids)
        selected: list = []
        for row in rows:
            if topic_ledger is not None and not topic_ledger.can_take(row):
                continue
            if self.take(row, exclude_ids=excluded):
                if topic_ledger is not None:
                    topic_ledger.take(row)
                selected.append(row)
            if len(selected) >= limit:
                break
        return selected


class TopicLedger:
    """Keep one topic from crowding out the rest of the digest."""

    def __init__(self, max_per_topic: int = 0) -> None:
        self.max_per_topic = max(0, int(max_per_topic or 0))
        self.counts: dict[str, int] = {}

    @staticmethod
    def topic_key(row: Any) -> str:
        sectors = [str(value) for value in (row.sectors or []) if value]
        if sectors:
            return sectors[0]
        return str(row.source_name or "unknown")

    def can_take(self, row: Any) -> bool:
        topic = self.topic_key(row)
        return not (
            self.max_per_topic and self.counts.get(topic, 0) >= self.max_per_topic
        )

    def take(self, row: Any) -> None:
        topic = self.topic_key(row)
        self.counts[topic] = self.counts.get(topic, 0) + 1


def _hot_section(
    config: dict,
    candidates: Sequence[Any],
    *,
    ledger: AssetLedger,
    topic_ledger: TopicLedger,
    exclude_ids: set[str],
) -> list:
    """The highest-scoring signals of the last 24 hours (§5.3 rule 1)."""
    limits = get_push_limits(config)
    if limits["hot"] <= 0:
        return []
    return ledger.select(
        candidates,
        limit=limits["hot"],
        exclude_ids=exclude_ids,
        topic_ledger=topic_ledger,
    )


def _watchlist_section(
    config: dict,
    candidates: Sequence[Any],
    watch_assets: set[str],
    *,
    ledger: AssetLedger,
    topic_ledger: TopicLedger,
    exclude_ids: set[str],
) -> list:
    """Watchlist signals, one per asset, on assets the hot section left free.

    The candidate window is wider than the limit because the asset dedup drops
    rows: a 10-signal limit may need to look past the first ten rows to fill
    ten distinct assets. The asset ledger is shared with the hot section, so
    an asset the hot section already spent cannot reappear here.
    """
    limits = get_push_limits(config)
    if limits["watchlist"] <= 0:
        return []
    if not watch_assets:
        return []
    matching = [
        row for row in candidates if watch_assets.intersection(set(row.assets or []))
    ]
    return ledger.select(
        matching,
        limit=limits["watchlist"],
        exclude_ids=exclude_ids,
        topic_ledger=topic_ledger,
    )


def _github_section(session, config: dict, *, exclude_ids: set[str]) -> list:
    """GitHub repos from the trending cache, minus what was pushed before.

    GitHub Trending lives outside the signal pool (§5.4), so it has its own
    push history and its own window. When the cache is empty the section
    falls back to legacy GitHub signal rows.
    """
    limits = get_push_limits(config)
    if limits["github"] <= 0:
        return []
    pushed = pushed_github_urls(session, window_days=limits["github_window_days"])
    cache = get_github_cache(session)
    if cache:
        # The cached GitHub payload is locale-independent raw text, so it is
        # shared by every locale unchanged. The payload keys are mapped to the
        # digest item shape the markdown renderer expects.
        candidates = sorted(
            (
                repo
                for repo in (cache.payload or [])
                if repo.get("url") not in pushed
            ),
            key=lambda repo: repo.get("first_seen_at") or "",
            reverse=True,
        )
        return [_github_item(repo) for repo in candidates][: limits["github"]]
    rows = [
        item
        for item in recent_signals(session, hours=24, is_github=True, limit=100)
        if item.id not in exclude_ids and item.url not in pushed
    ][: limits["github"]]
    return signal_items(session, rows)


def build_digest(session, config: dict, locale: str = "") -> dict:
    """Compile the push payload in one locale.

    Only title / summary / keywords switch language; ``content`` is never part
    of a digest, so nothing here needs the source-language body.
    """
    main_locale = get_output_language(config)
    content_locale = resolve_locale(locale, config)
    already_pushed = pushed_signal_ids(session)

    # One candidate pool for both signal sections. The sections differ in
    # *which* signals they prefer, not in how they compete: the hot section
    # takes the highest scores, the watchlist section takes what is left.
    # A shared pool is what keeps an asset from being spent twice (§5.3).
    limits = get_push_limits(config)
    candidates = recent_signals(
        session,
        hours=24,
        is_github=False,
        min_final_score=limits["min_score"],
        # The digest ranks by score, not by arrival: a 45-point roundup page
        # from the newest batch must not outrank a 100-point signal that
        # arrived an hour earlier.
        order="score",
        # Event, asset and topic gates can reject many high-scoring rows.
        # Widen the candidate window so the digest can still fill its slots
        # with distinct important signals instead of stopping at the first
        # cluster of duplicates.
        limit=min(500, max(100, (limits["hot"] + limits["watchlist"]) * 10)),
    )
    owner = "public" if get_mode(config) == "master" else SLAVER_WATCHLIST_OWNER
    watch_assets = {row.asset for row in list_watchlist(session, owner)}

    ledger = AssetLedger()
    topic_ledger = TopicLedger(limits["max_per_topic"])
    hot = _hot_section(
        config,
        candidates,
        ledger=ledger,
        topic_ledger=topic_ledger,
        exclude_ids=already_pushed,
    )
    selected = {row.id for row in hot}
    watch = _watchlist_section(
        config,
        candidates,
        watch_assets,
        ledger=ledger,
        topic_ledger=topic_ledger,
        exclude_ids=selected | already_pushed,
    )
    selected.update(row.id for row in watch)
    github = _github_section(session, config, exclude_ids=selected)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "locale": content_locale,
        "hot": signal_items(session, hot, locale=content_locale, main_locale=main_locale),
        "watchlist": signal_items(session, watch, locale=content_locale, main_locale=main_locale),
        "github": github,
    }


DIGEST_SECTIONS = {
    "en": ("# Investment Signals", "Hot Signals", "Watchlist Signals", "GitHub Trending"),
    "zh": ("# 投资信号", "热点资讯", "关注资讯", "GitHub 趋势"),
}

#: Platform notification subject, which is visible before the body is opened.
DIGEST_TITLES = {
    "en": "Investment Signals",
    "zh": "投资信号",
}


def digest_markdown(digest: dict) -> str:
    title, hot_label, watch_label, github_label = DIGEST_SECTIONS.get(
        str(digest.get("locale") or "en"), DIGEST_SECTIONS["en"]
    )
    lines = [title, ""]
    for heading, key in ((hot_label, "hot"), (watch_label, "watchlist"), (github_label, "github")):
        lines.extend([f"## {heading}", ""])
        for item in digest.get(key) or []:
            lines.append(f"- [{item.get('title', '')}]({item.get('url', '')}) — {item.get('summary', '')}")
        lines.append("")
    return "\n".join(lines).strip()


async def push_digest(session, config: dict, locale: str = "") -> dict:
    digest = build_digest(session, config, locale)
    set_setting(session, digest_key(digest["locale"]), json.dumps(digest, ensure_ascii=False))
    if digest["locale"] == get_output_language(config):
        set_setting(session, "latest_digest", json.dumps(digest, ensure_ascii=False))
    await send_to_platforms(
        digest_markdown(digest),
        config.get("push") or {},
        title=DIGEST_TITLES.get(str(digest["locale"]), DIGEST_TITLES["en"]),
    )
    # Written only after the digest was sent: the push history records what
    # was actually pushed, never merely pooled (§5.3 rule 2).
    record_github_pushes(session, digest.get("github") or [])
    record_signal_pushes(
        session,
        (digest.get("hot") or []) + (digest.get("watchlist") or []),
        locale=digest["locale"],
    )
    return digest
