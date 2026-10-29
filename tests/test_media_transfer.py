"""Detached ciphertext shares exact deletion, quotas and retention with message queues."""

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from test_attachments import decrypt, encrypted_send
from test_attachments import encrypted_room as _encrypted_room
from test_exact_delivery import headers, poll, reference, send
from test_exact_delivery import room as _room

from app.cleanup import _purge_once
from app.config import get_settings
from app.db import SessionLocal
from app.models import Chat, ClientSession, PendingMessage, SendRecord
from app.routers.chats import ws_manager
from scripts.deploy import backup_database

room = _room
encrypted_room = _encrypted_room


def upload(client, users, message_id="card", ciphertext="bWVkaWE=", sender=0):
    return client.post(f"/api/v1/chats/{users[0]['chat']['id']}/messages", json={
        "channel": "media", "client_message_id": message_id + ".media",
        "envelopes": [{"recipient_public_id": user["user"]["public_id"], "ciphertext": ciphertext}
                      for index, user in enumerate(users) if index != sender],
    }, headers=headers(users[sender]))


def media(client, users, recipient=1, sender=0, message_id="card", chat_id=None):
    return client.post(f"/api/v1/chats/{chat_id or users[0]['chat']['id']}/media/read", json={
        "sender_public_id": users[sender]["user"]["public_id"],
        "client_message_id": message_id + ".media",
    }, headers=headers(users[recipient]))


def ack(client, user, delivery):
    return client.post("/api/v1/ack/exact", headers=headers(user),
                       json={"messages": [reference(delivery)]})


def test_metadata_arrives_first_and_media_never_enters_poll_or_live_messages(room, monkeypatch):
    client, users = room()
    events = []

    async def push(user_id, value):
        events.append(value)

    monkeypatch.setattr(ws_manager, "is_online", lambda _: True)
    monkeypatch.setattr(ws_manager, "send_json", push)
    send(client, users, message_id="card")
    card = poll(client, users[1])["messages"][0]
    assert media(client, users).status_code == 404
    assert len(events) == 1
    assert upload(client, users, ciphertext="eA==").status_code == 200
    assert len(events) == 1
    assert poll(client, users[1])["messages"] == [card]
    response = media(client, users)
    assert response.status_code == 200
    assert response.json()["ciphertext"] == "eA=="
    assert response.headers["cache-control"] == "no-store"
    assert "content-encoding" not in response.headers


def test_metadata_and_media_have_independent_owner_bound_durable_acks(room):
    client, users = room(3)
    send(client, users, message_id="card")
    accepted = upload(client, users)
    assert accepted.status_code == 200
    card = poll(client, users[1])["messages"][0]
    file = media(client, users).json()
    assert ack(client, users[1], card).json()["deleted_messages"] == 1
    assert poll(client, users[1])["messages"] == []
    assert media(client, users).json() == file
    assert ack(client, users[2], file).json()["deleted_messages"] == 0
    assert ack(client, users[1], file).json()["deleted_messages"] == 1
    assert media(client, users).status_code == 404
    assert media(client, users, recipient=2).status_code == 200
    assert upload(client, users).json() == accepted.json()
    assert media(client, users).status_code == 404  # Retries never resurrect ACKed bytes.
    assert upload(client, users, ciphertext="eQ==").status_code == 409


def test_media_read_requires_recipient_chat_sender_and_live_session(room):
    client, users = room(3)
    _, other = room()
    send(client, users, message_id="card")
    assert upload(client, users).status_code == 200
    assert media(client, users, recipient=0).status_code == 404
    assert media(client, users, sender=2).status_code == 404
    assert media(client, users, chat_id=other[0]["chat"]["id"]).status_code == 404
    path = f"/api/v1/chats/{users[0]['chat']['id']}/media/read"
    assert client.post(path, json={"sender_public_id": users[0]["user"]["public_id"],
                                  "client_message_id": "card.media"}).status_code == 401

    async def revoke():
        async with SessionLocal() as session:
            record = await session.scalar(select(ClientSession).where(
                ClientSession.public_id == users[1]["user"]["public_id"]))
            record.revoked = True
            await session.commit()

    asyncio.run(revoke())
    assert media(client, users).status_code == 401


def test_media_needs_an_existing_own_parent_and_keeps_the_parents_expiry(room):
    client, users = room()
    assert upload(client, users).status_code == 409
    send(client, users, message_id="card")
    assert upload(client, users, sender=1).status_code == 409
    assert upload(client, users).status_code == 200

    async def expiry():
        async with SessionLocal() as session:
            records = list(await session.scalars(select(SendRecord).where(
                SendRecord.chat_id == users[0]["chat"]["id"]).order_by(SendRecord.id)))
            assert records[0].content_expires_at == records[1].content_expires_at
            for row in await session.scalars(select(PendingMessage).where(
                    PendingMessage.chat_id == users[0]["chat"]["id"])):
                row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await session.commit()

    asyncio.run(expiry())
    assert media(client, users).status_code == 404
    asyncio.run(_purge_once())
    assert poll(client, users[1])["messages"] == []


@pytest.mark.parametrize("field", [
    "max_pending_messages", "max_pending_bytes", "max_pending_per_sender",
    "max_pending_bytes_per_sender", "max_pending_per_chat", "max_pending_bytes_per_chat",
])
def test_media_uses_existing_capacity_without_evicting_metadata(room, monkeypatch, field):
    client, users = room()
    send(client, users, message_id="card")
    card = poll(client, users[1])["messages"]
    monkeypatch.setattr(get_settings(), field, 1)
    assert upload(client, users).status_code == 429
    assert poll(client, users[1])["messages"] == card
    assert media(client, users).status_code == 404


def test_channel_conflicts_cannot_reinterpret_or_replace_existing_ciphertext(room):
    client, users = room()
    send(client, users, message_id="card")
    response = client.post(f"/api/v1/chats/{users[0]['chat']['id']}/messages", json={
        "channel": "media", "client_message_id": "card",
        "envelopes": [{"recipient_public_id": users[1]["user"]["public_id"], "ciphertext": "eA=="}],
    }, headers=headers(users[0]))
    assert response.status_code == 409
    assert poll(client, users[1])["messages"][0]["client_message_id"] == "card"


def test_metadata_ack_does_not_prevent_later_media_upload(room):
    client, users = room()
    send(client, users, message_id="card")
    card = poll(client, users[1])["messages"][0]
    assert ack(client, users[1], card).json()["deleted_messages"] == 1
    assert upload(client, users).status_code == 200
    assert media(client, users).status_code == 200


def test_expired_parent_cannot_start_a_new_media_retention_window(room):
    client, users = room()
    send(client, users, message_id="card")

    async def expire():
        async with SessionLocal() as session:
            record = await session.scalar(select(SendRecord).where(
                SendRecord.chat_id == users[0]["chat"]["id"],
                SendRecord.client_message_id == "card"))
            record.content_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await session.commit()

    asyncio.run(expire())
    assert upload(client, users).status_code == 409
    assert media(client, users).status_code == 404


@pytest.mark.parametrize("size", [0, 1048576])
def test_real_encrypted_media_preserves_bytes_and_per_recipient_deletion(encrypted_room, size):
    client, users, keys = encrypted_room(3)
    send(client, users, message_id="card")
    body, payload = encrypted_send(users, keys, size=size, message_id="card.media")
    body["channel"] = "media"
    assert client.post(f"/api/v1/chats/{users[0]['chat']['id']}/messages",
                       headers=headers(users[0]), json=body).status_code == 200
    for index in (1, 2):
        response = media(client, users, recipient=index)
        assert payload["name"].encode() not in response.content
        assert decrypt(response.json(), keys[0], keys[index],
                       users[index]["user"]["public_id"]) == payload
        assert ack(client, users[index], response.json()).json()["deleted_messages"] == 1
    assert media(client, users).status_code == 404


def test_own_outbox_removal_covers_media_without_touching_another_sender(room):
    client, users = room()
    send(client, users, message_id="card")
    send(client, users, sender=1, message_id="other")
    assert upload(client, users).status_code == 200
    assert upload(client, users, sender=1, message_id="other").status_code == 200
    assert client.delete(f"/api/v1/chats/{users[0]['chat']['id']}/outbox",
                         headers=headers(users[0])).status_code == 200
    assert media(client, users).status_code == 404
    assert media(client, users, sender=1, recipient=0, message_id="other").status_code == 200


def test_closed_chat_rejects_media_reads_and_uploads(room):
    client, users = room()
    send(client, users, message_id="card")
    assert upload(client, users).status_code == 200

    async def close():
        async with SessionLocal() as session:
            chat = await session.get(Chat, users[0]["chat"]["id"])
            chat.closed_at = datetime.now(timezone.utc)
            await session.commit()

    asyncio.run(close())
    assert media(client, users).status_code == 410
    assert upload(client, users).status_code == 410


def test_managed_backup_omits_both_metadata_and_detached_media(room, tmp_path):
    client, users = room()
    send(client, users, message_id="card")
    assert upload(client, users).status_code == 200
    destination = tmp_path / "synthetic-backup.sqlite3"
    backup_database(Path(get_settings().db_path), destination)
    with sqlite3.connect(destination) as saved:
        assert saved.execute("SELECT count(*) FROM pending_messages").fetchone() == (0,)
        assert saved.execute("SELECT count(*) FROM send_records").fetchone()[0] >= 2
    assert media(client, users).status_code == 200
