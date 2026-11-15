from __future__ import annotations

import argparse
import asyncio

from src.app_logging import configure_logging
from src.config import (
    get_database_url,
    get_mode,
    get_schema,
    get_server_url,
    load_config,
    redact_database_url,
)
from src.db.bootstrap import bootstrap
from src.db.repositories import get_setting
from src.db.session import session_scope
from src.push.digest import push_digest
from src.server import run_server
from src.runtime import ensure_runtime_dirs


def _owner(config: dict) -> str:
    if get_mode(config) == "master":
        return "public"
    if not get_server_url(config):
        return str(config.get("owner") or "pending")
    owner = str(config.get("owner") or "")
    if not owner.startswith("public") and not owner.startswith("user:"):
        raise RuntimeError("slaver requires a verified manai-pic identity before fetching")
    return owner


async def run_fetch(config: dict) -> None:
    from src.orchestrator import run_slaver_fetch_cycle
    from src.pipeline import run_fetch_cycle

    with session_scope() as session:
        owner = _owner(config)
        if get_mode(config) == "master":
            report = await run_fetch_cycle(session, config, owner=owner, is_master=True)
        else:
            token = get_setting(session, "manai_pic_jwt", "")
            report = await run_slaver_fetch_cycle(session, config, owner=owner, user_id=owner.removeprefix("user:"), token=token)
    print(report)


async def run_push(config: dict) -> None:
    with session_scope() as session:
        print(await push_digest(session, config))


SERVICE_ACTIONS = ("install", "uninstall", "start", "stop", "restart", "status")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="news-agent")
    parser.add_argument(
        "command", choices=("serve", "fetch", "push", "check", "service")
    )
    # Only meaningful for ``service``; ``install.sh`` / ``install.ps1`` call
    # ``service install`` then ``service restart``.
    parser.add_argument("action", nargs="?", choices=SERVICE_ACTIONS)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=12301, type=int)
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)
    if args.command == "service" and not args.action:
        parser.error("service requires an action: " + ", ".join(SERVICE_ACTIONS))
    return args


def main() -> int:
    args = parse_args()
    if args.command == "service":
        from src import lifecycle

        print(getattr(lifecycle, args.action)())
        return 0
    config = load_config(args.config)
    paths = ensure_runtime_dirs()
    configure_logging(
        paths["logs"], int((config.get("log") or {}).get("retention_days", 30))
    )
    if args.command == "serve":
        run_server(args.host, args.port, args.config)
    elif args.command == "fetch":
        bootstrap(config)
        configure_logging(
            paths["logs"], int((config.get("log") or {}).get("retention_days", 30))
        )
        asyncio.run(run_fetch(config))
    elif args.command == "push":
        bootstrap(config)
        configure_logging(
            paths["logs"], int((config.get("log") or {}).get("retention_days", 30))
        )
        asyncio.run(run_push(config))
    else:
        print(
            {
                "mode": get_mode(config),
                "database": redact_database_url(get_database_url(config)),
                "schema": get_schema(config),
            }
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
