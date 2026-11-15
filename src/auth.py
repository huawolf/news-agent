"""manai-pic JWT verification and the local user projection (§3.1–§3.4).

The identity used for energy accounting always comes from a verified token —
never from a request body field. An unknown or unverifiable token is a ``401``;
there is no default user and no development bypass on master.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Sequence

import aiohttp
from sqlalchemy.orm import Session

from src.config import get_auth_config
from src.db.repositories import upsert_user_profile

logger = logging.getLogger(__name__)

USER_ME_PATH = "/api/v1/user/me"

# Energy rules (§3.4). Recorded only, not enforced: the balance may go negative.
ENERGY_PULL_NEW = -0.1
ENERGY_PULL_DUPLICATE = 0.0
ENERGY_REPORT_NEW = 1.0


class AuthError(Exception):
    """Token missing, malformed, or rejected by manai-pic."""

    def __init__(self, message: str = "unauthorized", status: int = 401):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass
class Identity:
    """The verified principal behind a request."""

    user_id: str
    username: str = ""
    email: str = ""
    phone: str = ""
    membership: str = ""
    is_admin: bool = False
    membership_expires_at: str = ""
    membership_plan: str = ""
    energy_quota: float | None = None
    energy_cached: float | None = None
    self_code: str = ""

    @property
    def owner(self) -> str:
        """Owner key for per-user rows, matching the ``slaver:<user_id>`` origin."""
        return f"user:{self.user_id}"


def extract_bearer(headers: Dict[str, str]) -> str:
    """Pull the raw token out of an ``Authorization: Bearer …`` header."""
    raw = ""
    for key, value in (headers or {}).items():
        if key.lower() == "authorization":
            raw = value or ""
            break
    raw = raw.strip()
    if not raw:
        return ""
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return raw


def _first_str(payload: Dict, *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return ""


def parse_identity(payload: Dict) -> Identity:
    """Build an :class:`Identity` from a ``/api/v1/user/me`` response.

    manai-pic wraps the user in ``data``; both shapes are accepted so a
    flat payload from a test double is not silently rejected.
    """
    body = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(body, dict):
        raise AuthError("malformed user payload")

    user_id = _first_str(body, "user_id", "userId", "id", "uid")
    if not user_id:
        raise AuthError("user payload carries no id")

    membership = body.get("membership")
    if isinstance(membership, dict):
        membership = _first_str(membership, "name", "level", "plan")
    elif membership is None:
        membership = ""

    membership_data = body.get("membership") if isinstance(body.get("membership"), dict) else {}
    return Identity(
        user_id=user_id,
        username=_first_str(body, "username", "user_name", "nickname", "name"),
        email=_first_str(body, "email", "email_address", "emailAddress"),
        phone=_first_str(body, "phone", "mobile", "phone_number", "phoneNumber"),
        membership=str(membership or ""),
        is_admin=False,
        membership_expires_at=_first_str(body, "membership_expires_at", "membershipExpiresAt")
        or _first_str(membership_data, "expires_at", "expiresAt"),
        membership_plan=_first_str(body, "membership_plan", "membershipPlan")
        or _first_str(membership_data, "plan", "name"),
        energy_quota=float(body["energy_quota"]) if isinstance(body.get("energy_quota"), (int, float)) else None,
        energy_cached=float(body["energy_cached"]) if isinstance(body.get("energy_cached"), (int, float)) else None,
        self_code=_first_str(body, "self_code", "selfCode"),
    )


async def verify_token(config: Dict, token: str) -> Identity:
    """Validate a manai-pic JWT against ``{main_url}/api/v1/user/me``."""
    if not token:
        raise AuthError("missing token")

    auth = get_auth_config(config)
    url = f"{auth['main_url']}{USER_ME_PATH}"
    timeout = aiohttp.ClientTimeout(total=auth["request_timeout"])

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url, headers={"Authorization": f"Bearer {token}"}
            ) as response:
                if response.status in (401, 403):
                    raise AuthError("token rejected by manai-pic")
                if response.status >= 400:
                    # An unreachable or broken manai-pic must not be mistaken for
                    # a valid identity; fail closed.
                    raise AuthError(f"manai-pic returned {response.status}")
                payload = await response.json(content_type=None)
    except AuthError:
        raise
    except aiohttp.ClientError as exc:
        logger.warning("manai-pic verification failed: %s", exc)
        raise AuthError("manai-pic unreachable") from exc
    except Exception as exc:  # noqa: BLE001 - any decode failure is an auth failure
        logger.warning("manai-pic verification error: %s", exc)
        raise AuthError("invalid token") from exc

    identity = parse_identity(payload if isinstance(payload, dict) else {})
    identity.is_admin = is_admin_identity(identity, auth.get("admins") or [])
    return identity


def is_admin_identity(identity: Identity, admins: Sequence[str]) -> bool:
    """Match a verified identity against the admin whitelist (§3.1).

    Each whitelist entry is a phone number or an email — the same two
    identifiers manai-pic itself trusts. Emails compare case-insensitively,
    phone numbers verbatim. An empty whitelist grants nobody.
    """
    email = identity.email.strip().casefold()
    phone = identity.phone.strip()
    for raw in admins:
        entry = str(raw or "").strip()
        if not entry:
            continue
        if "@" in entry:
            if email and entry.casefold() == email:
                return True
        elif phone and entry == phone:
            return True
    return False


def project(session: Session, identity: Identity) -> None:
    """Refresh the local ``user_profile`` projection.

    This is a cache for display and role checks, never an account store: the
    token is re-verified against manai-pic on every call regardless of what is
    written here.
    """
    upsert_user_profile(
        session,
        user_id=identity.user_id,
        username=identity.username,
        membership=identity.membership,
        energy_cached=identity.energy_cached,
        self_code=identity.self_code,
        is_admin=identity.is_admin,
    )


async def authenticate(session: Session, config: Dict, headers: Dict[str, str]) -> Identity:
    """Verify the request token and refresh the projection in one step."""
    identity = await verify_token(config, extract_bearer(headers))
    from src.membership import enrich_membership

    identity = enrich_membership(identity, config)
    project(session, identity)
    return identity


def require_admin(identity: Identity) -> None:
    if not identity.is_admin:
        raise AuthError("admin role required", status=403)


__all__ = [
    "AuthError",
    "ENERGY_PULL_DUPLICATE",
    "ENERGY_PULL_NEW",
    "ENERGY_REPORT_NEW",
    "Identity",
    "authenticate",
    "extract_bearer",
    "is_admin_identity",
    "parse_identity",
    "project",
    "require_admin",
    "verify_token",
]
