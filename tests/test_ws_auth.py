"""WebSocket authentication transport tests."""

import asyncio
import secrets
from datetime import datetime, timezone

import pytest
from csrf_client import TestClient
from message_key_fixtures import public_key
from starlette.websockets import WebSocket

from app.db import SessionLocal, init_db
from app.main import create_app
from app.models import Link, LinkType, User
from app.routers.sessions import issue
from app.routers.ws import _token_from_subprotocol
from app.ws_manager import manager


async def _receive() -> dict:
    return {"type": "websocket.disconnect"}


async def _send(message: dict) -> None:
    del message


def test_token_can_be_read_from_websocket_subprotocol() -> None:
    websocket = WebSocket(
        {
            "type": "websocket",
            "path": "/ws",
            "headers": [
                (
                    b"sec-websocket-protocol",
                    b"sideword.v1, sideword.auth.header.payload.signature",
                )
            ],
            "query_string": b"",
            "scheme": "ws",
            "server": ("test", 80),
            "client": ("test", 123),
            "subprotocols": [],
        },
        _receive,
        _send,
    )

    assert _token_from_subprotocol(websocket) == "header.payload.signature"


async def _create_websocket_user() -> tuple[User, Link]:
    await init_db()
    async with SessionLocal() as session:
        user = User(
            public_id=secrets.token_hex(6),
            display_name="WebSocket test",
            public_key=public_key(),
        )
        link = Link(
            token=secrets.token_urlsafe(32),
            link_type=LinkType.group,
            max_uses=0,
            uses_count=0,
            is_active=True,
        )
        session.add_all([user, link])
        await session.commit()
        await session.refresh(user)
        await session.refresh(link)
        return user, link


def _websocket_token(user, link):
    async def create():
        async with SessionLocal() as session:
            return (await issue(session, user, link.id, secrets.token_urlsafe(32)))["token"]
    return asyncio.run(create())


def test_websocket_accepts_private_auth_subprotocol() -> None:
    user, link = asyncio.run(_create_websocket_user())
    token = _websocket_token(user, link)

    with TestClient(create_app()) as client:
        with client.websocket_connect(
            "/ws",
            subprotocols=["sideword.v1", f"sideword.auth.{token}"],
        ) as websocket:
            hello = websocket.receive_json()

            assert websocket.accepted_subprotocol == "sideword.v1"
            assert hello["type"] == "hello"
            assert hello["user"] == user.public_id


def test_websocket_accepts_authentication_first_frame() -> None:
    user, link = asyncio.run(_create_websocket_user())
    token = _websocket_token(user, link)

    with TestClient(create_app()) as client:
        with client.websocket_connect(
            "/ws",
            subprotocols=["sideword.v1"],
        ) as websocket:
            websocket.send_json({"type": "auth", "token": token})
            hello = websocket.receive_json()

            assert websocket.accepted_subprotocol == "sideword.v1"
            assert hello["type"] == "hello"
            assert hello["user"] == user.public_id


def test_websocket_rejects_invalid_first_frame_session() -> None:
    with TestClient(create_app()) as client:
        with client.websocket_connect(
            "/ws",
            subprotocols=["sideword.v1"],
        ) as websocket:
            websocket.send_json({"type": "auth", "token": "not-a-valid-token"})

            assert websocket.receive_json() == {
                "type": "auth_error",
                "reason": "invalid session",
            }


def test_websocket_rejects_session_from_revoked_invite() -> None:
    user, link = asyncio.run(_create_websocket_user())
    token = _websocket_token(user, link)

    async def revoke() -> None:
        async with SessionLocal() as session:
            stored = await session.get(Link, link.id)
            assert stored is not None
            stored.is_active = False
            stored.revoked_at = datetime.now(timezone.utc)
            await session.commit()

    asyncio.run(revoke())

    with TestClient(create_app()) as client:
        with client.websocket_connect("/ws", subprotocols=["sideword.v1"]) as websocket:
            websocket.send_json({"type": "auth", "token": token})
            assert websocket.receive_json() == {
                "type": "auth_error",
                "reason": "invalid session",
            }


@pytest.mark.parametrize("invalidated", ["expiry", "revocation", "user", "jwt"])
def test_silent_socket_is_revalidated_before_push(invalidated, monkeypatch):
    user, link = asyncio.run(_create_websocket_user())
    token = _websocket_token(user, link)

    async def invalidate_and_push():
        async with SessionLocal() as session:
            stored = await session.get(Link, link.id)
            if invalidated == "expiry":
                stored.expires_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
            elif invalidated == "revocation":
                stored.revoked_at = datetime.now(timezone.utc)
            elif invalidated == "user":
                (await session.get(User, user.id)).is_active = False
            await session.commit()
        if invalidated == "jwt":
            import jwt

            def expired(_token):
                raise jwt.ExpiredSignatureError()

            monkeypatch.setattr("app.deps.decode_client_token", expired)
        await manager.send_json(user.id, {"type": "message", "message": "must not arrive"})

    with TestClient(create_app()) as client:
        # Legacy query authentication must remain accepted too.
        with client.websocket_connect(f"/ws?token={token}") as websocket:
            assert websocket.receive_json()["type"] == "hello"
            client.portal.call(invalidate_and_push)
            assert websocket.receive_json()["type"] == "auth_error"
            assert not manager.is_online(user.id)


def test_idle_socket_expires_without_client_ping(monkeypatch):
    monkeypatch.setattr("app.routers.ws.SESSION_CHECK_SECONDS", 0.02, raising=False)
    user, link = asyncio.run(_create_websocket_user())
    token = _websocket_token(user, link)

    async def expire():
        async with SessionLocal() as session:
            stored = await session.get(Link, link.id)
            stored.expires_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
            await session.commit()

    with TestClient(create_app()) as client:
        with client.websocket_connect("/ws", subprotocols=["sideword.v1"]) as websocket:
            websocket.send_json({"type": "auth", "token": token})
            assert websocket.receive_json()["type"] == "hello"
            client.portal.call(expire)
            assert websocket.receive_json()["type"] == "auth_error"


def test_valid_socket_keeps_receiving_and_pinging():
    user, link = asyncio.run(_create_websocket_user())
    token = _websocket_token(user, link)
    with TestClient(create_app()) as client:
        with client.websocket_connect("/ws", subprotocols=["sideword.v1"]) as websocket:
            websocket.send_json({"type": "auth", "token": token})
            assert websocket.receive_json()["type"] == "hello"
            client.portal.call(manager.send_json, user.id, {"type": "read", "read": {}})
            assert websocket.receive_json() == {"type": "read", "read": {}}
            websocket.send_text("ping")
            assert websocket.receive_text() == "pong"


def test_revoking_one_issuing_invite_does_not_invalidate_another_socket_session():
    user, first_link = asyncio.run(_create_websocket_user())
    _, second_link = asyncio.run(_create_websocket_user())
    first_token = _websocket_token(user, first_link)
    second_token = _websocket_token(user, second_link)

    async def revoke_and_push():
        async with SessionLocal() as session:
            (await session.get(Link, first_link.id)).revoked_at = datetime.now(timezone.utc)
            await session.commit()
        await manager.send_json(user.id, {"type": "read", "read": {}})

    with TestClient(create_app()) as client:
        with client.websocket_connect(f"/ws?token={first_token}") as first:
            assert first.receive_json()["type"] == "hello"
            with client.websocket_connect(f"/ws?token={second_token}") as second:
                assert second.receive_json()["type"] == "hello"
                client.portal.call(revoke_and_push)
                assert first.receive_json()["type"] == "auth_error"
                assert second.receive_json()["type"] == "read"
                assert manager.is_online(user.id)
    assert not manager.is_online(user.id)


def test_failed_backlog_load_removes_socket_registration(monkeypatch):
    user, link = asyncio.run(_create_websocket_user())
    token = _websocket_token(user, link)

    async def unavailable(*args):
        raise RuntimeError("backlog unavailable")

    monkeypatch.setattr("app.routers.ws._backlog_payload", unavailable)
    with TestClient(create_app()) as client:
        with pytest.raises(RuntimeError, match="backlog unavailable"):
            with client.websocket_connect(f"/ws?token={token}") as websocket:
                websocket.receive_json()
    assert not manager.is_online(user.id)
