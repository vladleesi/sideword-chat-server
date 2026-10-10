"""Explicit closure gates every session without deleting memberships or ciphertext."""

import asyncio
import base64
import json
import secrets
from datetime import datetime

import pytest
from csrf_client import TestClient
from message_key_fixtures import public_key
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from test_admin_lifecycle import activate, client, headers, make_link
from test_exact_delivery import poll, read, reference, send
from test_invite_lifecycle import join

from app import db
from app.db import SessionLocal
from app.main import create_app
from app.models import ChatMember, Link, LinkType, PendingMessage, ReadReceipt, User
from app.ws_manager import manager

__all__ = ["client"]


def group():
    async def create():
        async with SessionLocal() as session:
            link = Link(token=secrets.token_urlsafe(32), link_type=LinkType.group,
                        max_uses=0, uses_count=0, is_active=True)
            session.add(link)
            await session.commit()
            return link.id, link.token
    return asyncio.run(create())


def counts(chat_id):
    async def inspect():
        async with SessionLocal() as session:
            return tuple([await session.scalar(select(func.count(model.id)).where(
                model.chat_id == chat_id)) for model in (ChatMember, PendingMessage, ReadReceipt)])
    return asyncio.run(inspect())


def change(client, chat_id, action):
    response = client.post(f"/admin/chats/{chat_id}/{action}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/chats"


@pytest.mark.parametrize("revoke_invite", [False, True])
def test_closure_blocks_other_invite_sessions_and_preserves_archives_and_queues(
    client, revoke_invite,
):
    link_id, token = group()
    alice, bob = activate(client, token), activate(client, token)
    _, other_token = make_link()
    other_alice = join(client, other_token, alice)
    other_bob = join(client, other_token, bob)
    cid = alice["chat"]["id"]
    send(client, [alice, bob], message_id="read-before-close")
    delivered = poll(client, bob)["messages"][0]
    assert read(client, bob, reference(delivered)).status_code == 200
    send(client, [alice, bob], message_id="retained-before-close")
    before = counts(cid)
    assert before == (2, 1, 1)
    change(client, cid, "close")
    me = client.get("/api/v1/me", headers=headers(other_alice)).json()
    archive = next(chat for chat in me["chats"] if chat["id"] == cid)
    assert archive["closed_at"] is not None
    assert len(archive["participants"]) == 2
    assert client.get(f"/api/v1/chats/{cid}", headers=headers(other_alice)).json() == archive
    first_closed_at = archive["closed_at"]
    change(client, cid, "close")
    assert client.get(f"/api/v1/chats/{cid}", headers=headers(other_alice)).json()[
        "closed_at"] == first_closed_at
    if revoke_invite:
        assert client.post(f"/admin/links/{link_id}/revoke",
                           follow_redirects=False).status_code == 303
        assert client.get("/api/v1/me", headers=headers(alice)).status_code == 401
    for user in (other_alice, other_bob):
        assert client.get("/api/v1/me", headers=headers(user)).status_code == 200
        result = poll(client, user)
        assert result["messages"] == result["read_receipts"] == result["delivery_receipts"] == []
        response = client.post(f"/api/v1/chats/{cid}/messages", headers=headers(user), json={
            "client_message_id": "stale-client-send",
            "envelopes": [{"recipient_public_id": bob["user"]["public_id"],
                           "ciphertext": "eA=="}],
        })
        assert response.status_code == 410
        assert response.json()["detail"] == "conversation closed"
        assert client.post(f"/api/v1/chats/{cid}/messages/status", headers=headers(user), json={
            "client_message_ids": ["retained-before-close"],
        }).status_code == 410
        assert client.post(f"/api/v1/chats/{cid}/read/exact", headers=headers(user), json={
            "messages": [reference(delivered)], "viewed": True,
        }).status_code == 410
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "auth", "token": user["token"]})
            backlog = ws.receive_json()["backlog"]
            assert backlog["messages"] == backlog["read_receipts"] == backlog[
                "delivery_receipts"] == []
    assert counts(cid) == before
    # Closing room A never blocks messaging in room B.
    send(client, [other_alice, other_bob], message_id="unaffected-room")
    assert poll(client, other_bob)["messages"][0]["client_message_id"] == "unaffected-room"
    # Joining room C with the same identity cannot unfreeze A.
    _, third_token = make_link()
    third = join(client, third_token, other_alice)
    assert next(chat for chat in client.get("/api/v1/me", headers=headers(third)).json()[
        "chats"] if chat["id"] == cid)["closed_at"] is not None
    change(client, cid, "reopen")
    assert client.get(f"/api/v1/chats/{cid}", headers=headers(other_bob)).json()[
        "closed_at"] is None
    assert poll(client, other_bob)["messages"][0]["client_message_id"] == "retained-before-close"
    if revoke_invite:
        # Reopening a conversation never restores a revoked admission/session.
        assert client.get("/api/v1/me", headers=headers(alice)).status_code == 401
    send(client, [{**third, "chat": alice["chat"]}, other_bob], message_id="resumed-room")


def test_closure_preserves_late_durable_ack_contract_without_emitting_live_receipts(client):
    _, token = group()
    alice, bob = activate(client, token), activate(client, token)
    cid = alice["chat"]["id"]
    send(client, [alice, bob], message_id="persisted-before-close")
    send(client, [alice, bob], sender=1, message_id="withdraw-while-closed")
    delivery = poll(client, bob)["messages"][0]
    change(client, cid, "close")
    assert counts(cid) == (2, 2, 0)
    response = client.post("/api/v1/ack/exact", headers=headers(bob), json={
        "messages": [reference(delivery)], "confirm_delivery": True,
    })
    assert response.status_code == 200
    assert response.json()["deleted_messages"] == 1
    assert counts(cid) == (2, 1, 1)
    assert poll(client, alice)["delivery_receipts"] == []
    assert client.delete(f"/api/v1/chats/{cid}/outbox", headers=headers(bob)).json() == {
        "deleted": 1}
    assert counts(cid) == (2, 0, 1)


def test_closed_room_rejects_new_joins_but_allows_saved_participant_archive_reconnect(client):
    _, token = group()
    alice = activate(client, token)
    cid = alice["chat"]["id"]
    change(client, cid, "close")
    payload = {"token": token, "public_key": base64.b64encode(public_key()).decode(),
               "session_credential": secrets.token_urlsafe(32)}
    response = client.post("/api/v1/links/activate", json=payload)
    assert response.status_code == 410
    assert response.json()["detail"] == "conversation closed"
    assert counts(cid)[0] == 1
    assert "This conversation is closed." in client.get(f"/l/{token}").text
    reconnected = join(client, token, alice)
    assert reconnected["user"]["public_id"] == alice["user"]["public_id"]
    assert reconnected["chat"]["closed_at"] is not None
    change(client, cid, "reopen")
    assert client.post("/api/v1/links/activate", json=payload).status_code == 200


def test_existing_socket_drops_closed_room_delivery_and_presence_without_signout(client):
    _, token = group()
    alice, bob = activate(client, token), activate(client, token)
    _, other_token = make_link()
    other = join(client, other_token, bob)
    cid = alice["chat"]["id"]
    async def user_id():
        async with SessionLocal() as session:
            return await session.scalar(select(User.id).where(
                User.public_id == bob["user"]["public_id"]))
    uid = asyncio.run(user_id())
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": other["token"], "presence": True})
        assert ws.receive_json()["type"] == "hello"
        assert cid in {c["chat_id"] for c in ws.receive_json()["chats"]}
        change(client, cid, "close")
        event = ws.receive_json()
        assert event["type"] == "chat_state" and event["chat_id"] == cid
        assert event["closed_at"] is not None
        assert cid not in {c["chat_id"] for c in ws.receive_json()["chats"]}
        # Exercise the post-commit delivery race with a payload already prepared.
        for kind, field in (("message", "message"), ("read", "read"), ("delivered", "delivery")):
            client.portal.call(manager.send_json, uid, {"type": kind, field: {"chat_id": cid}})
        client.portal.call(manager.send_json, uid, {"type": "sentinel"})
        assert ws.receive_json() == {"type": "sentinel"}
        ws.send_text("ping")
        assert ws.receive_text() == "pong"
        # Opted-in ping also publishes a fresh presence snapshot.
        assert cid not in {c["chat_id"] for c in ws.receive_json()["chats"]}
        change(client, cid, "reopen")
        assert ws.receive_json()["closed_at"] is None
        assert cid in {c["chat_id"] for c in ws.receive_json()["chats"]}


def test_closure_requires_admin_and_export_import_preserves_closed_state(client):
    _, token = group()
    alice = activate(client, token)
    cid = alice["chat"]["id"]
    with TestClient(create_app()) as outsider:
        for action in ("close", "reopen"):
            assert outsider.post(f"/admin/chats/{cid}/{action}",
                                 follow_redirects=False).status_code in {401, 403}
    change(client, cid, "close")
    bundle = client.get("/admin/api/export").json()
    exported_link = next(link for link in bundle["links"] if link["token"] == token)
    chat = bundle["chats"][exported_link["chat_index"]]
    assert chat["closed_at"] is not None
    exported_link["chat_index"] = 0
    exported_link["token"] = secrets.token_urlsafe(32)
    transfer = {"version": 1, "users": [u for u in bundle["users"]
                if u["public_id"] == alice["user"]["public_id"]],
                "chats": [chat], "links": [exported_link]}
    def import_transfer():
        return client.post("/admin/api/import", files={
            "bundle": ("fixture.json", json.dumps(transfer), "application/json"),
        })
    assert import_transfer().status_code == 200
    imported = join(client, exported_link["token"], alice)
    assert datetime.fromisoformat(imported["chat"]["closed_at"]) == datetime.fromisoformat(
        chat["closed_at"])
    for invalid in ("invalid", 1, False):
        chat["closed_at"] = invalid
        assert import_transfer().status_code == 400
    assert client.post("/admin/chats/999999999/close", follow_redirects=False).status_code == 404


def test_additive_closure_migration_preserves_existing_chats_and_is_idempotent(
    tmp_path, monkeypatch,
):
    # Never point migrations at a running instance or the shared fixture database.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'closure-migration.sqlite3'}")
    monkeypatch.setattr(db, "engine", engine)
    async def migrate():
        try:
            async with engine.begin() as connection:
                await connection.execute(text("CREATE TABLE chats (id INTEGER PRIMARY KEY, "
                                              "chat_type VARCHAR(16), title VARCHAR(256), "
                                              "created_at DATETIME)"))
                await connection.execute(text("INSERT INTO chats VALUES "
                                              "(1, 'group', 'retained', '2026-01-01')"))
            await db.init_db()
            async with engine.begin() as connection:
                row = (await connection.execute(text(
                    "SELECT title,closed_at FROM chats WHERE id=1"))).one()
                assert row == ("retained", None)
                await connection.execute(text("UPDATE chats SET closed_at='2026-01-02' WHERE id=1"))
            await db.init_db()
            async with engine.connect() as connection:
                assert (await connection.execute(text(
                    "SELECT title,closed_at FROM chats WHERE id=1"))).one() == (
                        "retained", "2026-01-02")
        finally:
            await engine.dispose()
    asyncio.run(migrate())
