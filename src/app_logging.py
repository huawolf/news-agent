"""Portable daily directory logging."""
from __future__ import annotations

import logging
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path


LOG_NAMES = ("app", "fetch", "push", "web", "mcp", "audit")

ROUTES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("src.fetcher", ("app", "fetch")),
    ("src.adapters", ("app", "fetch")),
    ("src.adapters.crypto_crawler", ("app", "fetch")),
    ("src.pipeline", ("app", "fetch")),
    ("src.orchestrator", ("app", "fetch")),
    ("src.dedup", ("app", "fetch")),
    ("src.push", ("app", "push")),
    ("src.api", ("app", "web")),
    ("src.server", ("app", "web")),
    ("src.master_mcp", ("app", "mcp")),
    ("src.audit", ("app", "audit")),
)


def current_log_dir(log_root: Path) -> Path:
    path = log_root / datetime.now().strftime("%Y%m%d")
    path.mkdir(parents=True, exist_ok=True)
    return path


def cleanup_log_dirs(log_root: Path, retention_days: int = 30) -> None:
    if retention_days <= 0 or not log_root.exists():
        return
    cutoff = (datetime.now() - timedelta(days=retention_days - 1)).strftime("%Y%m%d")
    for child in log_root.iterdir():
        if child.is_dir() and child.name.isdigit() and len(child.name) == 8 and child.name < cutoff:
            shutil.rmtree(child, ignore_errors=True)


def _targets(name: str) -> tuple[str, ...]:
    for prefix, files in ROUTES:
        if name == prefix or name.startswith(prefix + "."):
            return files
    return ("app",)


class _RouteFilter(logging.Filter):
    def __init__(self, log_name: str):
        super().__init__()
        self.log_name = log_name

    def filter(self, record: logging.LogRecord) -> bool:
        return self.log_name in _targets(record.name)


def configure_logging(log_dir: Path, retention_days: int = 30) -> logging.Logger:
    """Attach subsystem file handlers and a console stream exactly once."""
    root = logging.getLogger()
    if any(
        isinstance(filter_, _RouteFilter)
        for handler in root.handlers
        for filter_ in handler.filters
    ):
        root.setLevel(logging.INFO)
        return logging.getLogger("news_agent")

    cleanup_log_dirs(log_dir, retention_days)
    daily_dir = current_log_dir(log_dir)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-5s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    for name in LOG_NAMES:
        handler = logging.FileHandler(daily_dir / f"{name}.log", encoding="utf-8")
        handler.setFormatter(formatter)
        handler.addFilter(_RouteFilter(name))
        root.addHandler(handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)
    root.setLevel(logging.INFO)
    return logging.getLogger("news_agent")
