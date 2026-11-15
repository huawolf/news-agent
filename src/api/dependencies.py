from __future__ import annotations

from fastapi import HTTPException, Request

from src.auth import AuthError, Identity, authenticate, require_admin
from src.db.session import session_scope


async def current_identity(request: Request) -> Identity:
    try:
        with session_scope() as session:
            identity = await authenticate(
                session,
                request.app.state.config,
                dict(request.headers),
            )
            return identity
    except AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc


async def admin_identity(request: Request) -> Identity:
    identity = await current_identity(request)
    try:
        require_admin(identity)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc
    return identity
