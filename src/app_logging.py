"""Portable daily directory logging."""

import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path


LOG_NAMES = ("app", "fetch", "push", "web", "mcp", "audit")


def current_log_dir(log_root: Path) -> Path:
    path = log_root / datetime.now().strftime("%Y%m%d")
    path.mkdir(parents=True, exist_ok=True)
    return path


def cleanup_log_dirs(log_root: Path, retention_days: int = 7) -> None:
    if retention_days <= 0 or not log_root.exists():
        return
    cutoff = (datetime.now() - timedelta(days=retention_days - 1)).strftime("%Y%m%d")
    for child in log_root.iterdir():
        if child.is_dir() and child.name.isdigit() and len(child.name) == 8 and child.name < cutoff:
            shutil.rmtree(child, ignore_errors=True)


def configure_logging(log_dir: Path, retention_days: int = 7) -> logging.Logger:
    logger = logging.getLogger("news_agent")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    cleanup_log_dirs(log_dir, retention_days)
    if getattr(logger, "_news_agent_configured", False):
        return logger

    daily_dir = current_log_dir(log_dir)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-5s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    for name in LOG_NAMES:
        handler = logging.FileHandler(daily_dir / f"{name}.log", encoding="utf-8")
        handler.setFormatter(formatter)
        logger.getChild(name).addHandler(handler)
        logger.getChild(name).setLevel(logging.INFO)
        logger.getChild(name).propagate = False
    logger._news_agent_configured = True
    return logger
