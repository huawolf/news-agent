"""Idempotent seed import.

Runs after Alembic and before any fetch scheduling. The rule is *insert only*:
a seed key that already exists is skipped in full, so sources a user disabled
or parameters they edited in the backoffice are never reset by an upgrade.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import RESOURCES_DIR
from src.db.models import AppState, FetchSource, Sector, SyncState

logger = logging.getLogger(__name__)


def rss_key(xml_url: str) -> str:
    """Seed key for an RSS source: URL hash only, never mixed with a user id.

    Keeping the key identical across instances is what lets an updated
    ``rss.opml`` recognise rows that already exist.
    """
    return hashlib.sha256(xml_url.strip().encode("utf-8")).hexdigest()[:32]


def _new_id() -> str:
    return uuid.uuid4().hex


def parse_rss_opml(opml_path: Path) -> List[Dict]:
    if not opml_path.exists():
        raise FileNotFoundError(f"rss seed not found: {opml_path}")

    root = ET.parse(opml_path).getroot()
    feeds: List[Dict] = []
    for outline in root.findall(".//outline[@type='rss']"):
        url = (outline.get("xmlUrl") or "").strip()
        if not url:
            continue
        feeds.append({"title": (outline.get("title") or url).strip(), "xml_url": url})
    return feeds


def _load_sectors(resources_dir: Path) -> Dict:
    path = resources_dir / "sectors.seed.json"
    if not path.exists():
        raise FileNotFoundError(f"sector seed not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _load_adapters(resources_dir: Path) -> Dict:
    path = resources_dir / "adapters.seed.json"
    if not path.exists():
        raise FileNotFoundError(f"adapter seed not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _load_feeds(resources_dir: Path) -> Dict:
    path = resources_dir / "feeds.seed.json"
    if not path.exists():
        raise FileNotFoundError(f"feed seed not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _load_retired(resources_dir: Path) -> Dict:
    """Retired sources: rows to disable, never to delete (§4.3).

    Sources are retired when they are permanently unavailable or only produce
    multi-event roundups, which conflict with the one-event-per-signal
    contract. The seed is *insert only*, so removing the source from
    ``rss.opml`` would not touch an existing instance; this list is what
    disables it there.
    """
    path = resources_dir / "retired_sources.seed.json"
    if not path.exists():
        return {"rss": []}
    return json.loads(path.read_text(encoding="utf-8"))


def seed_sectors(session: Session, payload: Optional[Dict] = None) -> int:
    payload = payload if payload is not None else _load_sectors(RESOURCES_DIR)
    existing = {row.name for row in session.scalars(select(Sector))}
    inserted = 0
    for item in payload.get("sectors", []):
        name = (item.get("name") or "").strip()
        if not name or name in existing:
            continue
        session.add(
            Sector(
                id=_new_id(),
                name=name,
                keywords=list(item.get("keywords") or []),
                sort_weight=int(item.get("sort_weight", 0)),
                enabled=True,
                sort_order=int(item.get("sort_order", 0)),
            )
        )
        existing.add(name)
        inserted += 1
    return inserted


def seed_fetch_sources(
    session: Session,
    owner: str,
    resources_dir: Optional[Path] = None,
) -> Dict[str, int]:
    resources_dir = resources_dir or RESOURCES_DIR
    existing = {
        (row.kind, row.key)
        for row in session.scalars(
            select(FetchSource).where(FetchSource.owner == owner)
        )
    }

    rss_inserted = 0
    for feed in parse_rss_opml(resources_dir / "rss.opml"):
        key = rss_key(feed["xml_url"])
        if ("rss", key) in existing:
            continue
        session.add(
            FetchSource(
                id=_new_id(),
                owner=owner,
                kind="rss",
                key=key,
                title=feed["title"],
                xml_url=feed["xml_url"],
                enabled=True,
                params={},
            )
        )
        existing.add(("rss", key))
        rss_inserted += 1

    adapter_inserted = 0
    for adapter in _load_adapters(resources_dir).get("adapters", []):
        kind = (adapter.get("kind") or "").strip()
        key = (adapter.get("key") or kind).strip()
        if not kind or (kind, key) in existing:
            continue
        session.add(
            FetchSource(
                id=_new_id(),
                owner=owner,
                kind=kind,
                key=key,
                title=adapter.get("title") or kind,
                xml_url="",
                enabled=bool(adapter.get("enabled", True)),
                params=dict(adapter.get("params") or {}),
            )
        )
        existing.add((kind, key))
        adapter_inserted += 1

    feed_inserted = 0
    for feed in _load_feeds(resources_dir).get("feeds", []):
        key = str(feed.get("key") or "").strip()
        url = str(feed.get("url") or "").strip()
        if not key or not url or ("feed", key) in existing:
            continue
        session.add(
            FetchSource(
                id=_new_id(),
                owner=owner,
                kind="feed",
                key=key,
                title=str(feed.get("title") or key).strip(),
                xml_url="",
                enabled=True,
                params={"url": url},
            )
        )
        existing.add(("feed", key))
        feed_inserted += 1

    return {"rss": rss_inserted, "adapters": adapter_inserted, "feeds": feed_inserted}


def ensure_singleton_rows(session: Session, seed_version: int) -> None:
    if session.get(SyncState, "singleton") is None:
        session.add(SyncState(id="singleton", scope="pull"))
    if session.get(AppState, "singleton") is None:
        session.add(AppState(id="singleton", seed_version=seed_version))


def retire_sources(
    session: Session,
    owner: str,
    resources_dir: Optional[Path] = None,
) -> int:
    """Disable the sources listed in ``retired_sources.seed.json``.

    The row is kept, not deleted: the instance's source catalogue stays
    auditable, and a user who wants the source back can re-enable it in the
    backoffice. Disabling is what stops the fetch cycle.
    """
    resources_dir = resources_dir or RESOURCES_DIR
    retired = _load_retired(resources_dir).get("rss") or []
    disabled = 0
    for entry in retired:
        xml_url = str(entry.get("xml_url") or "").strip()
        if not xml_url:
            continue
        row = session.scalar(
            select(FetchSource).where(
                FetchSource.owner == owner,
                FetchSource.kind == "rss",
                FetchSource.xml_url == xml_url,
            )
        )
        if row is None or not row.enabled:
            continue
        row.enabled = False
        disabled += 1
        logger.info(
            "retired source disabled: %s (%s)",
            row.title,
            entry.get("reason") or "no reason given",
        )
    return disabled


def run_seed(session: Session, owner: str = "public", resources_dir: Optional[Path] = None) -> Dict:
    """Import all default data. Raises on any failure so startup aborts.

    ``seed_version`` is display only (§4.3): the import is driven by per-key
    existence, never by comparing versions.
    """
    resources_dir = resources_dir or RESOURCES_DIR
    sectors_payload = _load_sectors(resources_dir)

    stats = {
        "sectors": seed_sectors(session, sectors_payload),
        "fetch_sources": seed_fetch_sources(session, owner, resources_dir),
    }
    stats["retired_sources"] = retire_sources(session, owner, resources_dir)
    ensure_singleton_rows(session, int(sectors_payload.get("version", 0) or 0))
    session.commit()

    has_rss = session.scalar(
        select(FetchSource.id)
        .where(FetchSource.owner == owner, FetchSource.kind == "rss")
        .limit(1)
    )
    if has_rss is None:
        raise RuntimeError("seed produced no RSS sources; refusing to continue")
    if session.scalar(select(Sector.id).limit(1)) is None:
        raise RuntimeError("seed produced no sectors; refusing to continue")

    logger.info(
        "seed complete: +%s sectors, +%s rss sources, +%s adapters",
        stats["sectors"],
        stats["fetch_sources"]["rss"],
        stats["fetch_sources"]["adapters"],
    )
    return stats
