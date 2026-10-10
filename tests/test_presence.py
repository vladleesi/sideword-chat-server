"""Ephemeral presence transitions on the authenticated WebSocket transport."""

import asyncio
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from anyio import sleep_forever
from csrf_client import TestClient
from starlette.websockets import WebSocketDisconnect
from test_ws_auth import _create_websocket_user, _websocket_token

from app.db import SessionLocal
from app.main import create_app
from app.models import Chat, ChatMember, ChatType, ClientSession, Link, User
from app.routers.sessions import issue
from app.ws_manager import HEARTBEAT_TIMEOUT_SECONDS, ConnectionManager, manager


@pytest.fixture(autouse=True)
def isolated_connection_registry(monkeypatch):
    # Each TestClient owns a separate event loop, unlike the shared server runner.
    # Give each test its own ephemeral registry and asyncio locks.
    current = ConnectionManager()
    monkeypatch.setattr("app.routers.ws.manager", current)
    monkeypatch.setattr(f"{__name__}.manager", current)
    yield
    assert not current._connections
    assert not current._validators
    assert not current._presence
    assert not current._activity
    assert not current._public_ids


async def room():
    alice, alice_link = await _create_websocket_user()
    bob, bob_link = await _create_websocket_user()
    outsider, outsider_link = await _create_websocket_user()
    async with SessionLocal() as session:
        chat = Chat(chat_type=ChatType.group)
        private_chat = Chat(chat_type=ChatType.personal)
        session.add_all([chat, private_chat])
        await session.flush()
        session.add_all([
            ChatMember(chat_id=chat.id, user_id=alice.id),
            ChatMember(chat_id=chat.id, user_id=bob.id),
            ChatMember(chat_id=private_chat.id, user_id=outsider.id),
        ])
        # A consumed invite still authorizes its existing participant.
        (await session.get(Link, alice_link.id)).is_active = False
        await session.commit()
        return (chat.id, private_chat.id, alice, bob, outsider,
                alice_link, bob_link, outsider_link)


@contextmanager
def connect(client, user, link, *, presence=True, token=None):
    with client.websocket_connect("/ws", subprotocols=["sideword.v1"]) as ws:
        ws.send_json({"type": "auth", "token": token or _websocket_token(user, link),
                      "presence": presence})
        assert ws.receive_json()["type"] == "hello"
        yield ws


def snapshot(ws, chat_id, online):
    payload = ws.receive_json()
    assert payload["type"] == "presence"
    assert 0 < payload["valid_for_ms"] <= 35000
    chat = next(item for item in payload["chats"] if item["chat_id"] == chat_id)
    assert set(chat["online"]) == {user.public_id for user in online}
    return payload


def test_connect_disconnect_reconnect_and_multiple_connections():
    cid, _, alice, bob, _, al, bl, _ = asyncio.run(room())
    with TestClient(create_app()) as client, connect(client, alice, al) as a:
        snapshot(a, cid, [alice])
        with connect(client, bob, bl) as b:
            snapshot(b, cid, [alice, bob])
            snapshot(a, cid, [alice, bob])
            with connect(client, bob, bl) as second:
                snapshot(second, cid, [alice, bob])
                snapshot(b, cid, [alice, bob])
                snapshot(a, cid, [alice, bob])
                second.close()
                snapshot(b, cid, [alice, bob])
                snapshot(a, cid, [alice, bob])
            b.close()
            snapshot(a, cid, [alice])
        with connect(client, bob, bl) as reconnected:
            snapshot(reconnected, cid, [alice, bob])
            snapshot(a, cid, [alice, bob])
            reconnected.close()
            snapshot(a, cid, [alice])
    assert not manager.is_online(alice.id)
    assert not manager.is_online(bob.id)


def test_presence_is_scoped_to_current_membership_and_legacy_stream_is_unchanged():
    cid, private_cid, alice, bob, outsider, al, bl, ol = asyncio.run(room())

    async def remove_membership():
        from sqlalchemy import delete

        async with SessionLocal() as session:
            await session.execute(delete(ChatMember).where(
                ChatMember.user_id == alice.id, ChatMember.chat_id == cid
            ))
            await session.commit()
        await manager.refresh_presence()

    with TestClient(create_app()) as client, connect(client, alice, al) as a:
        initial = snapshot(a, cid, [alice])
        assert initial["chats"][0]["participants"] == sorted([alice.public_id, bob.public_id])
        with connect(client, outsider, ol) as other:
            payload = snapshot(other, private_cid, [outsider])
            assert [item["chat_id"] for item in payload["chats"]] == [private_cid]
            assert outsider.public_id not in str(snapshot(a, cid, [alice]))
            with connect(client, bob, bl, presence=False) as legacy:
                snapshot(a, cid, [alice, bob])
                snapshot(other, private_cid, [outsider])
                legacy.send_text("ping")
                assert legacy.receive_text() == "pong"
                client.portal.call(remove_membership)
                assert a.receive_json()["chats"] == []
                snapshot(other, private_cid, [outsider])


def test_cancelled_socket_still_notifies_remaining_observers():
    cid, _, alice, bob, _, al, bl, _ = asyncio.run(room())
    with TestClient(create_app()) as client, connect(client, alice, al) as a:
        snapshot(a, cid, [alice])
        with connect(client, bob, bl) as b:
            snapshot(b, cid, [alice, bob])
            snapshot(a, cid, [alice, bob])
        # TestClient cancels the ASGI task immediately after sending disconnect.
        snapshot(a, cid, [alice])


@pytest.mark.parametrize("phase", ["before_presence", "after_presence"])
def test_cancelled_socket_during_initialization_cleans_up_and_notifies(monkeypatch, phase):
    cid, _, alice, bob, _, al, bl, _ = asyncio.run(room())
    enable_presence = manager.enable_presence

    async def pause_initialization(websocket):
        is_bob = manager._public_ids.get(websocket) == bob.public_id
        if is_bob and phase == "before_presence":
            await sleep_forever()
        await enable_presence(websocket)
        if is_bob and phase == "after_presence":
            await sleep_forever()

    monkeypatch.setattr(manager, "enable_presence", pause_initialization)
    with TestClient(create_app()) as client, connect(client, alice, al) as a:
        snapshot(a, cid, [alice])
        with connect(client, bob, bl) as b:
            if phase == "after_presence":
                snapshot(b, cid, [alice, bob])
                snapshot(a, cid, [alice, bob])
        assert not client.portal.call(manager.is_online, bob.id)
        snapshot(a, cid, [alice])
        a.send_text("ping")
        assert a.receive_text() == "pong"
        snapshot(a, cid, [alice])


def test_replaced_public_identity_cannot_inherit_presence_from_old_socket():
    cid, _, alice, bob, _, al, bl, _ = asyncio.run(room())
    replacement_id = f"replacement-{bob.public_id}"

    async def replace_identity_and_refresh():
        async with SessionLocal() as session:
            # Emulate identity replacement/row-ID reuse before socket cleanup.
            (await session.get(User, bob.id)).public_id = replacement_id
            await session.commit()
        await manager.refresh_presence(next(iter(manager._connections[alice.id])))

    with TestClient(create_app()) as client, connect(client, alice, al) as a:
        snapshot(a, cid, [alice])
        with connect(client, bob, bl) as b:
            snapshot(b, cid, [alice, bob])
            snapshot(a, cid, [alice, bob])
            client.portal.call(replace_identity_and_refresh)
            payload = snapshot(a, cid, [alice])
            assert replacement_id in payload["chats"][0]["participants"]


def test_stale_socket_is_removed_and_observers_receive_offline_transition():
    cid, _, alice, bob, _, al, bl, _ = asyncio.run(room())

    async def expire_peer():
        for ws in manager._connections[bob.id]:
            manager._activity[ws] -= HEARTBEAT_TIMEOUT_SECONDS + 1
        await manager.refresh_presence()

    with TestClient(create_app()) as client, connect(client, alice, al) as a:
        snapshot(a, cid, [alice])
        with connect(client, bob, bl) as b:
            snapshot(b, cid, [alice, bob])
            snapshot(a, cid, [alice, bob])
            client.portal.call(expire_peer)
            snapshot(a, cid, [alice])
            with pytest.raises(WebSocketDisconnect) as error:
                b.receive_json()
            assert error.value.code == 1001
            assert not manager.is_online(bob.id)
            assert not any(ws in manager._activity for ws in manager._presence
                           if ws not in manager._validators)


def test_heartbeat_timeout_cleans_up_without_any_other_connection(monkeypatch):
    monkeypatch.setattr("app.ws_manager.HEARTBEAT_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr("app.routers.ws.SESSION_CHECK_SECONDS", 0.02)
    cid, _, alice, _, _, al, _, _ = asyncio.run(room())
    with TestClient(create_app()) as client, connect(client, alice, al) as a:
        snapshot(a, cid, [alice])
        with pytest.raises(WebSocketDisconnect) as error:
            a.receive_json()
        assert error.value.code == 1001
    assert not manager.is_online(alice.id)


def test_ping_renews_only_its_socket_and_snapshots_do_not_outlive_peer_heartbeats():
    cid, _, alice, bob, _, al, bl, _ = asyncio.run(room())

    async def age_sockets():
        for user in (alice, bob):
            for ws in manager._connections[user.id]:
                manager._activity[ws] -= HEARTBEAT_TIMEOUT_SECONDS - 5

    with TestClient(create_app()) as client, connect(client, alice, al) as a:
        snapshot(a, cid, [alice])
        with connect(client, bob, bl) as b:
            snapshot(b, cid, [alice, bob])
            snapshot(a, cid, [alice, bob])
            client.portal.call(age_sockets)
            a.send_text("ping")
            assert a.receive_text() == "pong"
            assert snapshot(a, cid, [alice, bob])["valid_for_ms"] <= 5000
            b.send_text("ping")
            assert b.receive_text() == "pong"
            assert snapshot(b, cid, [alice, bob])["valid_for_ms"] > 30000


def test_each_session_is_checked_and_last_valid_session_preserves_online_state():
    cid, _, alice, bob, _, al, bl, second_link = asyncio.run(room())

    async def revoke_one():
        async with SessionLocal() as session:
            (await session.get(Link, bl.id)).revoked_at = datetime.now(timezone.utc)
            await session.commit()
        await manager.refresh_presence()

    with TestClient(create_app()) as client, connect(client, alice, al) as a:
        snapshot(a, cid, [alice])
        with connect(client, bob, bl) as first:
            snapshot(first, cid, [alice, bob])
            snapshot(a, cid, [alice, bob])
            with connect(client, bob, second_link) as second:
                snapshot(second, cid, [alice, bob])
                snapshot(first, cid, [alice, bob])
                snapshot(a, cid, [alice, bob])
                client.portal.call(revoke_one)
                assert first.receive_json()["type"] == "auth_error"
                snapshot(second, cid, [alice, bob])
                snapshot(a, cid, [alice, bob])
                second.close()
                snapshot(a, cid, [alice])


@pytest.mark.parametrize("invalidate", ["revoked", "expired"])
def test_renewable_sessions_have_independent_presence_lifetimes(invalidate):
    cid, _, alice, bob, _, al, bl, _ = asyncio.run(room())

    async def sessions():
        async with SessionLocal() as session:
            first = await issue(session, bob, bl.id, secrets.token_urlsafe(32))
            second = await issue(session, bob, bl.id, secrets.token_urlsafe(32))
            return first, second

    first_session, second_session = asyncio.run(sessions())

    async def invalidate_one():
        async with SessionLocal() as session:
            stored = await session.get(ClientSession, first_session["session_id"])
            if invalidate == "revoked":
                stored.revoked = True
            else:
                stored.expires_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
            await session.commit()
        await manager.refresh_presence()

    with TestClient(create_app()) as client, connect(client, alice, al) as a:
        snapshot(a, cid, [alice])
        with connect(client, bob, bl, token=first_session["token"]) as first:
            snapshot(first, cid, [alice, bob])
            snapshot(a, cid, [alice, bob])
            with connect(client, bob, bl, token=second_session["token"]) as second:
                snapshot(second, cid, [alice, bob])
                snapshot(first, cid, [alice, bob])
                snapshot(a, cid, [alice, bob])
                client.portal.call(invalidate_one)
                assert first.receive_json()["type"] == "auth_error"
                snapshot(second, cid, [alice, bob])
                snapshot(a, cid, [alice, bob])
                second.close()
                snapshot(a, cid, [alice])


def test_presence_disabled_returns_no_presence_and_invalid_auth_cannot_subscribe(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "presence_enabled", False)
    _, _, alice, _, _, al, _, _ = asyncio.run(room())
    with TestClient(create_app()) as client:
        with connect(client, alice, al) as a:
            a.send_text("ping")
            assert a.receive_text() == "pong"
        with client.websocket_connect("/ws") as invalid:
            invalid.send_json({"type": "auth", "token": "invalid", "presence": True})
            assert invalid.receive_json()["type"] == "auth_error"
