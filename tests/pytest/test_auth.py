"""Authentication regression tests (§3.1–§3.4).

The contract that matters: an identity exists only when manai-pic accepted the
token. Missing, malformed, rejected and unverifiable tokens must all fail closed
— no default user, no development bypass.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src import auth
from src.auth import AuthError, Identity, authenticate, parse_identity
from src.db.bootstrap import run_migrations
from src.db.repositories import energy_balance
from src.db.session import (
    create_backend_indexes,
    dispose_engine,
    get_engine,
    session_scope,
)


@pytest.fixture
def db(tmp_path):
    dispose_engine()
    database_url = f"sqlite:///{tmp_path / 'auth.db'}"
    run_migrations(database_url)
    engine = get_engine(database_url, "news_agent")
    create_backend_indexes(engine)
    yield engine
    dispose_engine()


@pytest.fixture
def config():
    return {"mode": "master", "auth": {"main_url": "http://127.0.0.1:5678", "request_timeout": 5}}


class _Response:
    def __init__(self, status: int, payload=None):
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Session:
    """Minimal aiohttp.ClientSession stand-in returning a queued response."""

    def __init__(self, response, error=None, recorder=None):
        self._response = response
        self._error = error
        self._recorder = recorder

    def get(self, url, headers=None):
        if self._recorder is not None:
            self._recorder.append({"url": url, "headers": headers})
        if self._error is not None:
            raise self._error
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _patch_session(monkeypatch, response=None, error=None, recorder=None):
    def factory(*args, **kwargs):
        return _Session(response, error, recorder)

    monkeypatch.setattr(auth.aiohttp, "ClientSession", factory)


# --------------------------------------------------------------------------- header parsing


# --------------------------------------------------------------------------- payload parsing


def test_parse_identity_accepts_wrapped_and_flat_payloads():
    wrapped = parse_identity({"data": {"id": 7, "username": "amy", "is_admin": True}})
    assert (wrapped.user_id, wrapped.username, wrapped.is_admin) == ("7", "amy", False)

    flat = parse_identity({"user_id": "9", "nickname": "bo"})
    assert (flat.user_id, flat.username, flat.is_admin) == ("9", "bo", False)


def test_parse_identity_reads_account_energy_and_invite_code():
    identity = parse_identity({"data": {"id": 1, "energy_cached": 42, "self_code": "INVITE7"}})

    assert identity.energy_cached == 42
    assert identity.self_code == "INVITE7"


def test_owner_key_matches_the_slaver_origin_format():
    assert Identity(user_id="42").owner == "user:42"


# --------------------------------------------------------------------------- verification


async def test_verify_token_calls_manai_pic_user_me(config, monkeypatch):
    recorder = []
    monkeypatch.delenv("NEWS_AGENT_ADMIN_EMAIL", raising=False)
    _patch_session(
        monkeypatch,
        response=_Response(200, {"data": {"id": "7", "username": "amy", "is_admin": True}}),
        recorder=recorder,
    )
    identity = await auth.verify_token(config, "tok")
    assert identity.user_id == "7"
    assert identity.is_admin is False
    assert recorder[0]["url"] == "http://127.0.0.1:5678/api/v1/user/me"
    assert recorder[0]["headers"]["Authorization"] == "Bearer tok"


async def test_admin_fails_closed_when_email_is_unconfigured(config, monkeypatch):
    monkeypatch.delenv("NEWS_AGENT_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("NEWS_AGENT_ADMINS", raising=False)
    _patch_session(
        monkeypatch,
        response=_Response(200, {"data": {"id": "7", "email": "admin@example.com", "is_admin": True}}),
    )
    assert (await auth.verify_token(config, "tok")).is_admin is False


async def test_admin_whitelist_matches_a_phone_number(config, monkeypatch):
    monkeypatch.delenv("NEWS_AGENT_ADMIN_EMAIL", raising=False)
    monkeypatch.setenv("NEWS_AGENT_ADMINS", "00000000000,admin@example.com")
    _patch_session(
        monkeypatch,
        response=_Response(200, {"data": {"id": "7", "phone": "00000000000", "is_admin": False}}),
    )
    assert (await auth.verify_token(config, "tok")).is_admin is True


async def test_missing_token_never_reaches_manai_pic(config, monkeypatch):
    _patch_session(monkeypatch, response=_Response(200, {"data": {"id": "7"}}), recorder=[])
    with pytest.raises(AuthError):
        await auth.verify_token(config, "")


@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_token_raises(config, monkeypatch, status):
    _patch_session(monkeypatch, response=_Response(status))
    with pytest.raises(AuthError):
        await auth.verify_token(config, "bad")


async def test_server_error_fails_closed(config, monkeypatch):
    """A broken manai-pic must not be mistaken for a valid identity."""
    _patch_session(monkeypatch, response=_Response(500))
    with pytest.raises(AuthError):
        await auth.verify_token(config, "tok")


# --------------------------------------------------------------------------- projection


async def test_authenticate_writes_the_local_projection(db, config, monkeypatch):
    _patch_session(monkeypatch, response=_Response(200, {"data": {"id": "7", "username": "amy", "energy_cached": 35, "self_code": "AMY123"}}))
    with session_scope() as session:
        identity = await authenticate(session, config, {"Authorization": "Bearer tok"})

    from src.db.models import UserProfile

    with session_scope() as session:
        row = session.get(UserProfile, identity.user_id)
        assert row is not None
        assert row.username == "amy"
        assert row.energy_cached == 35
        assert row.self_code == "AMY123"
        assert row.last_seen_at is not None


# --------------------------------------------------------------------------- roles and energy


def test_energy_is_recorded_against_the_verified_user_only(db):
    from src.db.repositories import record_energy

    with session_scope() as session:
        record_energy(session, "7", "pull", auth.ENERGY_PULL_NEW)
        record_energy(session, "7", "pull", auth.ENERGY_PULL_DUPLICATE)
        record_energy(session, "7", "report", auth.ENERGY_REPORT_NEW)

    with session_scope() as session:
        assert energy_balance(session, "7") == pytest.approx(0.9)
        assert energy_balance(session, "8") == 0.0
