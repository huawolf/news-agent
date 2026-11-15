from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from src.config import get_server_url


class MasterSyncError(RuntimeError):
    pass


class MasterAuthError(MasterSyncError):
    pass


class MasterSyncClient:
    def __init__(self, config: dict, token: str, transport: httpx.AsyncBaseTransport | None = None):
        base = get_server_url(config)
        if not base:
            raise MasterSyncError("server_url is empty")
        self.base_url = base
        self.token = token.strip()
        self.http = httpx.AsyncClient(transport=transport, timeout=30.0, headers={"Authorization": f"Bearer {self.token}"})

    async def close(self) -> None:
        await self.http.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        try:
            response = await self.http.get(f"{self.base_url}{path}", params=params)
        except httpx.HTTPError as exc:
            raise MasterSyncError(str(exc)) from exc
        if response.status_code in (401, 403):
            raise MasterAuthError("master rejected token")
        if response.status_code >= 400:
            raise MasterSyncError(f"master returned {response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise MasterSyncError("master returned invalid JSON")
        return payload

    async def pull_signals_page(
        self,
        since: datetime,
        page: int,
        *,
        locale: str = "",
        translations_since: datetime | None = None,
        translations_page: int = 1,
    ) -> dict:
        """Fetch one page of the shared pool plus its late translations.

        Both streams share the endpoint but page independently: the signal
        cursor walks first insertions, the translation cursor walks text that
        finished after the signal was already pulled.
        """
        params: dict[str, Any] = {"since": since.isoformat(), "page": page}
        if locale:
            params["locale"] = locale
        if translations_since is not None:
            params["translations_since"] = translations_since.isoformat()
            params["translations_page"] = translations_page
        return await self._get("/api/sync/signals", params)

    async def list_sources(self) -> list[dict]:
        payload = await self._get("/api/server/sources")
        return list(payload.get("items") or [])

    async def github_cache(self) -> dict:
        return await self._get("/api/server/github-trending")
