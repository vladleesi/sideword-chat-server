"""Invite changes never replace identity/membership authorization or erase history."""

import asyncio
import secrets
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from test_admin_lifecycle import activate, client, headers, make_link
from test_exact_delivery import poll, send

from app.db import SessionLocal
from app.models import ChatMember, Link, User
from app.ws_manager import manager

__all__ = ["client"]


def join(client, token, user, *, authenticated=True, resume=None, credential=None):
    response = client.post("/api/v1/links/activate", json={
        "token": token,
        "public_key": user["user"]["public_key"],
        "resume_credential": resume or secrets.token_urlsafe(32),
        "session_credential": credential or secrets.token_urlsafe(32),
    }, headers=headers(user) if authenticated else {})
    assert response.status_code == 200
    return response.json()


def invalidate(client, link_id, boundary):
    if boundary == "revocation":
        assert client.post(f"/admin/links/{link_id}/revoke",
                           follow_redirects=False).status_code == 303
    else:
        async def expire():
            async with SessionLocal() as session:
                link = await session.get(Link, link_id)
                link.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                await session.commit()
        asyncio.run(expire())


@pytest.mark.parametrize("boundary", ["revocation", "expiration"])
def test_new_invite_cannot_restore_membership_after_only_session_is_invalid(client, boundary):
    link_id, token = make_link()
    old = activate(client, token)
    resume, credential = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    renewable = join(client, token, old, resume=resume, credential=credential)
    peer = activate(client, token)
    send(client, [peer, old], message_id="preserved-queued-ciphertext")
    invalidate(client, link_id, boundary)
    _, new_token = make_link()
    payload = {"token": new_token, "public_key": old["user"]["public_key"]}
    for user in (old, renewable, peer):
        assert client.get("/api/v1/me", headers=headers(user)).status_code == 401
        assert client.get("/api/v1/poll", headers=headers(user)).status_code == 401
        assert client.post("/api/v1/links/activate", json=payload,
                           headers=headers(user)).status_code == 401
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "auth", "token": user["token"]})
            assert ws.receive_json()["type"] == "auth_error"
    # Credential-only recovery of the old invite remains unavailable.
    response = client.post("/api/v1/links/activate", json={
        "token": token, "public_key": old["user"]["public_key"],
        "resume_credential": resume,
    })
    assert response.status_code == 410
    assert response.json()["detail"] == (
        "link revoked" if boundary == "revocation" else "link expired")
    assert client.post("/api/v1/sessions/refresh", json={
        "credential": credential, "next_credential": secrets.token_urlsafe(32),
    }).status_code == 401
    fresh = join(client, new_token, old, authenticated=False)
    assert fresh["user"]["public_id"] != old["user"]["public_id"]
    me = client.get("/api/v1/me", headers=headers(fresh)).json()
    assert [chat["id"] for chat in me["chats"]] == [fresh["chat"]["id"]]
    assert client.get(f"/api/v1/chats/{old['chat']['id']}",
                      headers=headers(fresh)).status_code == 404
    assert poll(client, fresh)["messages"] == []
    assert client.post(f"/api/v1/chats/{old['chat']['id']}/messages",
                       headers=headers(fresh), json={
                           "client_message_id": "unauthorized-send",
                           "envelopes": [{"recipient_public_id": peer["user"]["public_id"],
                                          "ciphertext": "eA=="}],
                       }).status_code == 404
    # Membership and queued ciphertext survive; revocation is not deletion.
    async def original_membership():
        async with SessionLocal() as session:
            return await session.scalar(select(ChatMember.id).join(User).where(
                User.public_id == old["user"]["public_id"],
                ChatMember.chat_id == old["chat"]["id"]))
    assert asyncio.run(original_membership()) is not None
    if boundary == "revocation":
        assert client.post(f"/admin/links/{link_id}/reactivate",
                           follow_redirects=False).status_code == 303
        assert poll(client, old)["messages"][0]["client_message_id"] == (
            "preserved-queued-ciphertext")


@pytest.mark.parametrize("boundary", ["revocation", "expiration"])
def test_other_live_session_intentionally_preserves_old_memberships(client, boundary):
    link_id, token = make_link()
    alice, bob = activate(client, token), activate(client, token)
    _, other_token = make_link()
    other_alice = join(client, other_token, alice)
    other_bob = join(client, other_token, bob)
    assert other_alice["user"]["public_id"] == alice["user"]["public_id"]
    invalidate(client, link_id, boundary)
    assert client.get("/api/v1/me", headers=headers(alice)).status_code == 401
    _, new_token = make_link()
    restored = join(client, new_token, other_alice)
    me = client.get("/api/v1/me", headers=headers(restored)).json()
    assert {chat["id"] for chat in me["chats"]} == {
        alice["chat"]["id"], other_alice["chat"]["id"], restored["chat"]["id"]}
    # Send in the original room, rather than the most recently activated room.
    send(client, [{**restored, "chat": alice["chat"]}, other_bob],
         message_id="old-room-after-invite-change")
    assert poll(client, other_bob)["messages"][0]["client_message_id"] == (
        "old-room-after-invite-change")
    send(client, [alice, {**other_bob, "chat": bob["chat"]}], sender=1,
         message_id="old-room-reply")
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": restored["token"]})
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["backlog"]["messages"][0]["client_message_id"] == "old-room-reply"


def test_admin_revocation_closes_only_invalid_socket_and_allows_reconnect(client):
    link_id, token = make_link()
    alice = activate(client, token)
    _, other_token = make_link()
    other = join(client, other_token, alice)
    with client.websocket_connect("/ws") as old_ws:
        old_ws.send_json({"type": "auth", "token": alice["token"]})
        assert old_ws.receive_json()["type"] == "hello"
        with client.websocket_connect("/ws") as live_ws:
            live_ws.send_json({"type": "auth", "token": other["token"]})
            assert live_ws.receive_json()["type"] == "hello"
            invalidate(client, link_id, "revocation")
            assert old_ws.receive_json()["type"] == "auth_error"
            client.portal.call(manager.send_json, int_identity(alice), {"type": "test"})
            assert live_ws.receive_json() == {"type": "test"}
    with client.websocket_connect("/ws") as reconnected:
        reconnected.send_json({"type": "auth", "token": other["token"]})
        assert reconnected.receive_json()["type"] == "hello"


def int_identity(user):
    async def lookup():
        async with SessionLocal() as session:
            return await session.scalar(select(User.id).where(
                User.public_id == user["user"]["public_id"]))
    return asyncio.run(lookup())
