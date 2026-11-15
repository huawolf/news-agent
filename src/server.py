"""News Agent HTTP entrypoint."""

from src.api.app import create_app


def run_server(
    host: str = "127.0.0.1",
    port: int = 12301,
    config_path: str | None = None,
) -> None:
    import uvicorn

    uvicorn.run(create_app(config_path), host=host, port=port, log_level="info")


__all__ = ["create_app", "run_server"]
