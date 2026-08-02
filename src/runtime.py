"""Cross-platform runtime paths for the local News Agent service."""

import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def user_data_dir() -> Path:
    override = os.environ.get("NEWS_AGENT_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return PROJECT_ROOT


def default_config_path() -> Path:
    override = os.environ.get("NEWS_AGENT_CONFIG")
    if override:
        return Path(override).expanduser().resolve()
    local_config = PROJECT_ROOT / "config.json"
    if local_config.exists():
        return local_config
    return user_data_dir() / "config.json"


def ensure_runtime_dirs() -> dict[str, Path]:
    base = user_data_dir()
    paths = {
        "base": base,
        "logs": base / "logs",
        "runs": base / "runs",
        "backups": base / "backups",
        "news_data": base / "news-data",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths
