"""Cross-platform runtime paths for the local News Agent service."""

import json
import os
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


def write_config_value(key: str, value, path: Path | None = None) -> Path:
    """Merge one top-level ``key`` into ``config.json`` via an atomic replace.

    Used by the preference settings so a saved value survives a restart without
    rewriting the whole runtime config. A missing or unreadable file starts from
    an empty object rather than failing the request.
    """
    target = Path(path) if path is not None else default_config_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data[key] = value
    content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    temporary = target.with_name(f".{target.name}.tmp")
    if os.name != "nt":
        mode = target.stat().st_mode & 0o777 if target.exists() else 0o600
        temporary.touch(exist_ok=True)
        temporary.chmod(mode)
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, target)
    return target


def ensure_runtime_dirs() -> dict[str, Path]:
    base = user_data_dir()
    paths = {
        "base": base,
        "logs": base / "logs",
        "database": base / "data",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths
