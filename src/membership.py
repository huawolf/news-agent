from __future__ import annotations

import os
from datetime import datetime, timezone
from functools import lru_cache

from sqlalchemy import create_engine, text


@lru_cache(maxsize=4)
def _engine(url: str):
    return create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=2)


def enrich_membership(identity, config: dict):
    auth = config.get("auth") or {}
    url = str(auth.get("membership_database_url") or os.environ.get("NEWS_AGENT_MEMBERSHIP_DATABASE_URL") or "").strip()
    if not url:
        return identity
    try:
        with _engine(url).connect() as connection:
            user = connection.execute(text("SELECT id FROM users WHERE id = :user_id"), {"user_id": identity.user_id}).first()
            if user is None:
                return identity
            row = connection.execute(text("""
                SELECT MAX(end_time) AS expiry
                FROM orders
                WHERE user_id = :user_id
                  AND plan_type = 'MEMBER'
                  AND status = 'SUCCESS'
                  AND end_time > :now
            """), {"user_id": user.id, "now": datetime.now(timezone.utc)}).first()
            identity.membership = "pro" if row and row.expiry else "free"
            identity.membership_expires_at = row.expiry.isoformat() if row and row.expiry else ""
            identity.membership_plan = "MEMBER" if row and row.expiry else "free"
    except Exception:
        identity.membership = identity.membership or ""
    return identity
