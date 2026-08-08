"""In-memory cache for processed/scored news entries on the server."""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from pathlib import Path
from src.storage import get_fetch_file, read_entries
from src.config import get_timezone

logger = logging.getLogger("news_agent.server_cache")


class InMemoryNewsCache:
    def __init__(self):
        self._entries: List[Dict] = []
        self._initialized = False

    def initialize(self, data_dir: str, config: Dict):
        """Load past news from disk to populate the 24h cache."""
        if self._initialized:
            return

        tz = get_timezone(config)
        now = datetime.now(tz)

        # Load from disk (last 2 days to ensure coverage of 24h)
        disk_entries = []
        for i in range(2):
            d = now.date() - timedelta(days=i)
            fetch_file = get_fetch_file(d, data_dir)
            if Path(fetch_file).exists():
                try:
                    for entry in read_entries(fetch_file):
                        disk_entries.append(entry)
                except Exception as e:
                    logger.error(f"Failed to read historical entries from {fetch_file}: {e}")

        self.update(disk_entries, config)
        self._initialized = True
        logger.info(f"Initialized in-memory news cache with {len(self._entries)} entries.")

    def update(self, new_entries: List[Dict], config: Dict):
        """Update cache with new entries and prune entries older than 24 hours."""
        tz = get_timezone(config)
        now = datetime.now(tz)
        cutoff = now - timedelta(hours=24)

        # Merge: use link as the unique key to avoid duplicates
        merged = {e.get("link"): e for e in self._entries if e.get("link")}

        for entry in new_entries:
            link = entry.get("link")
            if link:
                merged[link] = entry

        # Filter entries within 24h
        valid_entries = []
        for entry in merged.values():
            fetched_at_str = entry.get("fetched_at")
            if not fetched_at_str:
                continue
            try:
                # Parse fetched_at ISO format
                fetched_at = datetime.fromisoformat(fetched_at_str)
                # Ensure timezone aware
                if fetched_at.tzinfo is None:
                    fetched_at = fetched_at.replace(tzinfo=tz)
                else:
                    fetched_at = fetched_at.astimezone(tz)

                if fetched_at >= cutoff:
                    valid_entries.append(entry)
            except Exception:
                # Fallback to keep it if parsing fails but it has fetched_at
                valid_entries.append(entry)

        # Sort by fetched_at descending
        self._entries = sorted(valid_entries, key=lambda x: x.get("fetched_at", ""), reverse=True)

    def get_news(self, hours: int, config: Dict) -> List[Dict]:
        """Retrieve news within the past N hours (max 24)."""
        tz = get_timezone(config)
        now = datetime.now(tz)

        # Enforce max 24 hours constraint
        lookback_hours = min(24, max(1, int(hours)))
        start_time = now - timedelta(hours=lookback_hours)

        results = []
        for entry in self._entries:
            fetched_at_str = entry.get("fetched_at")
            if not fetched_at_str:
                continue
            try:
                fetched_at = datetime.fromisoformat(fetched_at_str)
                if fetched_at.tzinfo is None:
                    fetched_at = fetched_at.replace(tzinfo=tz)
                else:
                    fetched_at = fetched_at.astimezone(tz)

                if fetched_at >= start_time:
                    results.append(entry)
            except Exception:
                pass

        return results


# Global cache instance
server_news_cache = InMemoryNewsCache()
