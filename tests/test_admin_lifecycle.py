import asyncio
import base64
import secrets
from datetime import datetime, timedelta, timezone

import pytest
from csrf_client import TestClient
from message_key_fixtures import public_key
from sqlalchemy import select

from app.db import SessionLocal
from app.main import create_app
from app.models import Chat, ChatMember, Link, LinkType, PendingMessage, ReadReceipt, User
from app.routers.ws import _resolve_user


@pytest.fixture
def client():
    with TestClient(create_app()) as client:
        assert client.post(
            "/admin/login", data={"username": "admin", "password": "adminpass"},
            follow_redirects=False,
        ).status_code == 303
        yield client


def make_link(hours=None):
    async def create():
        async with SessionLocal() as session:
            link = Link(
                token=secrets.token_urlsafe(32), link_type=LinkType.personal,
                max_uses=2, uses_count=0, is_active=True,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=hours)
                if hours is not None else None,
            )
            session.add(link)
            await session.commit()
            return link.id, link.token
    return asyncio.run(create())


def activate(client, token):
    response = client.post("/api/v1/links/activate", json={
        "token": token,
        "public_key": base64.b64encode(public_key()).decode(),
        "session_credential": secrets.token_urlsafe(32),
        "display_name": "Test participant",
    })
    assert response.status_code == 200
    return response.json()


def headers(user):
    return {"Authorization": f"Bearer {user['token']}"}


def test_restore_full_link_restores_access_without_allowing_third_user(client):
    link_id, token = make_link(1)
    user = activate(client, token)
    activate(client, token)
    client.post(f"/admin/links/{link_id}/revoke")
    assert client.get("/api/v1/me", headers=headers(user)).status_code == 401
    result = client.post(f"/admin/links/{link_id}/reactivate", follow_redirects=False)
    assert result.status_code == 303
    assert client.get("/api/v1/me", headers=headers(user)).status_code == 200
    assert client.post("/api/v1/links/activate", json={
        "token": token,
        "public_key": base64.b64encode(public_key()).decode(),
        "session_credential": secrets.token_urlsafe(32),
    }).status_code == 410


def test_deadline_and_expired_restore(client):
    link_id, token = make_link(1)
    user = activate(client, token)
    data = client.get("/api/v1/me", headers=headers(user)).json()
    assert data["access_expires_at"].endswith("Z")
    assert data["server_time"] < data["access_expires_at"]

    async def expire():
        async with SessionLocal() as session:
            link = await session.get(Link, link_id)
            link.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await session.commit()
        async with SessionLocal() as session:
            assert await _resolve_user(session, user["token"]) is None
    asyncio.run(expire())
    assert client.get("/api/v1/me", headers=headers(user)).status_code == 401
    response = client.post(f"/admin/links/{link_id}/reactivate")
    assert response.headers["content-type"].startswith("text/html")
    assert "This link has expired" in response.text
    _, unlimited = make_link()
    data = client.get("/api/v1/me", headers=headers(activate(client, unlimited))).json()
    assert data["access_expires_at"] is not None


def test_delete_link_invalidates_sessions_and_cannot_be_restored(client):
    link_id, token = make_link()
    user = activate(client, token)
    assert client.post(f"/admin/links/{link_id}/delete", data={
        "confirm": "cancel",
    }).status_code == 400
    assert client.get("/api/v1/me", headers=headers(user)).status_code == 200
    client.post(f"/admin/links/{link_id}/delete", data={"confirm": "delete"})
    assert client.get("/api/v1/me", headers=headers(user)).status_code == 401
    assert client.post(f"/admin/links/{link_id}/reactivate").status_code == 404
    assert token not in client.get("/admin/links").text
    assert token not in client.get("/admin/api/export").text
    new_id, _ = make_link()
    assert new_id != link_id


def test_delete_user_removes_memberships_and_pending_messages(client):
    _, token = make_link()
    alice, bob = activate(client, token), activate(client, token)
    chat_id = alice["chat"]["id"]
    assert client.post(f"/api/v1/chats/{chat_id}/messages", headers=headers(alice), json={
        "client_message_id": "cleanup-test",
        "envelopes": [{"recipient_public_id": bob["user"]["public_id"], "ciphertext": "eA=="}],
    }).status_code == 200

    async def user_id():
        async with SessionLocal() as session:
            return await session.scalar(select(User.id).where(
                User.public_id == alice["user"]["public_id"]
            ))
    uid = asyncio.run(user_id())
    client.post(f"/admin/users/{uid}/delete", data={"confirm": "delete"})
    assert client.get("/api/v1/me", headers=headers(alice)).status_code == 401
    async def check():
        async with SessionLocal() as session:
            assert await session.get(User, uid) is None
            assert await session.scalar(select(ChatMember.id).where(
                ChatMember.user_id == uid
            )) is None
            assert await session.scalar(select(PendingMessage.id).where(
                PendingMessage.chat_id == chat_id
            )) is None
    asyncio.run(check())


def test_bulk_cleanup_preserves_active_users_and_consumed_links(client):
    full_id, full_token = make_link()
    alice, bob = activate(client, full_token), activate(client, full_token)
    revoked_id, revoked_token = make_link()
    client.post(f"/admin/links/{revoked_id}/revoke")
    client.post("/admin/links/cleanup", data={"confirm": "delete"})
    page = client.get("/admin/links").text
    assert full_token in page
    assert revoked_token not in page
    assert client.get("/api/v1/me", headers=headers(alice)).status_code == 200

    async def block():
        async with SessionLocal() as session:
            user = await session.scalar(select(User).where(
                User.public_id == bob["user"]["public_id"]
            ))
            user.is_active = False
            await session.commit()
    asyncio.run(block())
    client.post("/admin/users/cleanup", data={"confirm": "delete"})
    assert client.get("/api/v1/me", headers=headers(alice)).status_code == 200
    assert client.get("/api/v1/me", headers=headers(bob)).status_code == 401
    assert client.post(f"/admin/links/{full_id}/reactivate").status_code == 200


def test_cleanup_requires_admin_authentication():
    with TestClient(create_app()) as anonymous:
        for path in ("/admin/users/cleanup", "/admin/links/cleanup",
                     "/admin/users/1/delete", "/admin/links/1/delete"):
            assert anonymous.post(path, data={"confirm": "delete"}).status_code == 401


@pytest.mark.parametrize("bulk", [False, True])
def test_delete_chat_cleans_dependencies_and_preserves_other_chats(client, bulk):
    link_id, token = make_link()
    alice, bob = activate(client, token), activate(client, token)
    chat_id = alice["chat"]["id"]
    _, other_token = make_link()
    other = activate(client, other_token)
    assert client.post(f"/api/v1/chats/{chat_id}/messages", headers=headers(alice), json={
        "client_message_id": "chat-deletion-test",
        "envelopes": [{"recipient_public_id": bob["user"]["public_id"], "ciphertext": "eA=="}],
    }).status_code == 200
    path = "/admin/chats/delete-selected" if bulk else f"/admin/chats/{chat_id}/delete"
    data = {"confirm": "delete", "ids": [chat_id]}
    assert client.post(path, data=data, follow_redirects=False).status_code == 303
    assert client.get("/api/v1/me", headers=headers(alice)).status_code == 401
    assert client.get("/api/v1/me", headers=headers(other)).status_code == 200

    async def check():
        async with SessionLocal() as session:
            assert await session.get(Chat, chat_id) is None
            assert await session.get(Chat, other["chat"]["id"]) is not None
            for model in (ChatMember, PendingMessage, ReadReceipt):
                remaining = await session.scalar(select(model.id).where(model.chat_id == chat_id))
                assert remaining is None
            link = await session.get(Link, link_id)
            assert link.is_deleted and link.chat_id is None
    asyncio.run(check())


def test_selected_link_deletion_only_removes_selected_rows(client):
    first_id, first_token = make_link()
    second_id, second_token = make_link()
    _, keep_token = make_link()
    response = client.post("/admin/links/delete-selected", data={
        "ids": [first_id, second_id], "confirm": "delete",
    })
    assert response.status_code == 200
    assert first_token not in response.text and second_token not in response.text
    assert keep_token in response.text


def test_selected_deletion_requires_authentication_and_confirmation(client):
    link_id, token = make_link()
    with TestClient(create_app()) as anonymous:
        assert anonymous.post("/admin/links/delete-selected", data={
            "ids": [link_id], "confirm": "delete",
        }).status_code == 401
    assert client.post("/admin/links/delete-selected", data={
        "ids": [link_id], "confirm": "cancel",
    }).status_code == 400
    assert token in client.get("/admin/links").text
