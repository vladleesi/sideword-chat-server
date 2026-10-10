"""Session issuance and rotation boundaries use the isolated pytest database."""

import asyncio
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_exact_delivery import headers
from test_exact_delivery import room as _room
from test_security_stages import activate_session, rotate

from app.db import SessionLocal
from app.main import create_app
from app.models import ClientSession, Link, RefreshUse, User
from app.routers import links, sessions
from app.security import decode_client_token

room = _room


async def invalidate(claims, boundary):
    async with SessionLocal() as session:
        user = await session.get(User, int(claims["sub"]))
        link = await session.get(Link, claims["lid"])
        if boundary == "inactive":
            user.is_active = False
        elif boundary == "replaced":
            user.public_id = secrets.token_hex(6)
        elif boundary == "revoked":
            link.revoked_at = datetime.now(timezone.utc)
        elif boundary == "deleted":
            link.is_deleted = True
        elif boundary == "expired":
            link.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        elif boundary == "session_expired":
            record = await session.get(ClientSession, claims["sid"])
            record.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()


def count_sessions(claims):
    async def count():
        async with SessionLocal() as session:
            return await session.scalar(select(func.count(ClientSession.id)).where(
                ClientSession.user_id == int(claims["sub"])))
    return asyncio.run(count())


def assert_denied(client, token):
    assert client.get("/api/v1/me", headers=headers({"token": token})).status_code == 401
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": token})
        assert ws.receive_json()["type"] == "auth_error"


@pytest.mark.parametrize("boundary", ["inactive", "replaced", "revoked", "deleted", "expired"])
@pytest.mark.parametrize("retry", [False, True])
def test_issuance_rechecks_admission_after_commit(room, monkeypatch, boundary, retry):
    client, users = room()
    user = users[0]
    claims = decode_client_token(user["token"])
    credential = secrets.token_urlsafe(32)
    if retry:
        assert activate_session(client, user, credential).status_code == 200
    before = count_sessions(claims)
    original = sessions.issue

    async def interleaved_issue(*args, **kwargs):
        # This separate connection commits after admission/authentication, with
        # the request still holding its earlier ORM user object.
        await invalidate(claims, boundary)
        return await original(*args, **kwargs)

    monkeypatch.setattr(links, "issue", interleaved_issue)
    response = activate_session(client, user, credential)
    assert response.status_code == 401
    assert "token" not in response.json()
    assert count_sessions(claims) == before


@pytest.mark.parametrize("boundary", ["session_revocation", "session_expiry", "token_expiry"])
@pytest.mark.parametrize("retry", [False, True])
def test_authenticated_activation_rechecks_session_at_issuance(room, monkeypatch, boundary, retry):
    client, users = room()
    claims = decode_client_token(users[0]["token"])
    credential = secrets.token_urlsafe(32)
    if retry:
        assert activate_session(client, users[0], credential).status_code == 200
    before = count_sessions(claims)
    original = sessions.issue

    async def interleaved_issue(*args, **kwargs):
        if boundary != "token_expiry":
            async with SessionLocal() as session:
                record = await session.get(ClientSession, claims["sid"])
                if boundary == "session_revocation":
                    record.revoked = True
                else:
                    record.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                await session.commit()
        else:
            class ExpiredClock:
                @staticmethod
                def now(tz=None):
                    return datetime.fromtimestamp(claims["exp"] + 1, timezone.utc)
            monkeypatch.setattr(jwt.api_jwt, "datetime", ExpiredClock)
        return await original(*args, **kwargs)

    monkeypatch.setattr(links, "issue", interleaved_issue)
    assert activate_session(client, users[0], credential).status_code == 401
    assert count_sessions(claims) == before


@pytest.mark.parametrize("boundary", [
    "inactive", "revoked", "deleted", "expired", "session_expired",
])
def test_refresh_and_grace_retry_reject_invalid_subject(room, boundary):
    client, users = room()
    old, new = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    issued = activate_session(client, users[0], old).json()
    assert rotate(client, old, new).status_code == 200
    asyncio.run(invalidate(decode_client_token(issued["token"]), boundary))
    assert rotate(client, old, new).status_code == 401
    assert rotate(client, new, secrets.token_urlsafe(32)).status_code == 401
    assert_denied(client, issued["token"])


@pytest.mark.parametrize("race", ["identical", "conflicting", "revoke"])
def test_concurrent_refresh_and_revocation_preserve_replay_evidence(room, race):
    client, users = room()
    old, new = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    issued = activate_session(client, users[0], old).json()
    ready = Barrier(2)

    def attempt(index):
        with TestClient(create_app()) as worker:
            ready.wait(timeout=15)
            if race == "revoke" and index == 1:
                return worker.delete("/api/v1/sessions/" + issued["session_id"],
                                     headers=headers(users[0]))
            successor = secrets.token_urlsafe(32) if race == "conflicting" and index else new
            return rotate(worker, old, successor)

    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(attempt, range(2)))
    codes = [response.status_code for response in responses]
    if race == "identical":
        assert codes == [200, 200]
        assert all(response.json()["session_id"] == issued["session_id"] for response in responses)
        async def evidence_count():
            async with SessionLocal() as session:
                return await session.scalar(select(func.count(RefreshUse.token_hash)).where(
                    RefreshUse.session_id == issued["session_id"]))
        assert asyncio.run(evidence_count()) == 1
        assert rotate(client, new, secrets.token_urlsafe(32)).status_code == 200
    else:
        if race == "conflicting":
            assert sorted(codes) == [200, 401]
        else:
            assert codes[0] in {200, 401} and codes[1] == 200
        assert_denied(client, issued["token"])
        for response in responses:
            if response.status_code == 200 and "token" in response.json():
                assert_denied(client, response.json()["token"])
        assert rotate(client, new, secrets.token_urlsafe(32)).status_code == 401


@pytest.mark.parametrize("preexisting", [False, True])
def test_invite_caps_issued_and_refreshed_deadlines_and_me(room, preexisting):
    client, users = room()
    claims = decode_client_token(users[0]["token"])
    deadline = datetime.now(timezone.utc) + timedelta(minutes=2)
    old, new = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    if preexisting:
        assert activate_session(client, users[0], old).status_code == 200

    async def set_expiry():
        async with SessionLocal() as session:
            link = await session.get(Link, claims["lid"])
            link.expires_at = deadline
            await session.commit()
    asyncio.run(set_expiry())
    response = activate_session(client, users[0], old)
    assert response.status_code == 200
    issued = response.json()
    retried = activate_session(client, users[0], old).json()
    for data in (issued, retried, rotate(client, old, new).json()):
        assert datetime.fromisoformat(data["access_expires_at"]) == deadline
        assert datetime.fromisoformat(data["session_expires_at"]) == deadline

    async def shorten_session():
        async with SessionLocal() as session:
            record = await session.get(ClientSession, issued["session_id"])
            if preexisting:
                assert sessions.utc(record.expires_at) > deadline
            else:
                assert sessions.utc(record.expires_at) == deadline
            record.expires_at = deadline - timedelta(minutes=1)
            await session.commit()
    asyncio.run(shorten_session())
    me = client.get("/api/v1/me", headers=headers(issued)).json()
    assert datetime.fromisoformat(me["access_expires_at"]) == deadline - timedelta(minutes=1)
    assert me["access_expires_at"] == me["session_expires_at"]
