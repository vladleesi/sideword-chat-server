"""Synthetic encrypted files exercise the existing relay lifecycle and resource boundaries."""

import asyncio
import base64
import json
import secrets
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from csrf_client import TestClient
from sqlalchemy import delete, select
from test_exact_delivery import headers, poll, reference
from test_exact_delivery import room as _room

from app.config import get_settings
from app.db import SessionLocal
from app.delivery import queued_messages
from app.guards import SecurityGuards
from app.main import create_app
from app.models import Chat, Link, LinkType, PendingMessage, User
from app.routers.ws import _backlog_payload
from app.ws_manager import ConnectionManager

room = _room


def b64(value):
    return base64.b64encode(value).decode("ascii")


def raw_key(key):
    return key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
    )


@pytest.fixture
def encrypted_room():
    with TestClient(create_app()) as client:
        def create_room(size=2):
            async def create():
                async with SessionLocal() as session:
                    link = Link(token=secrets.token_urlsafe(32), max_uses=size,
                                link_type=LinkType.personal if size == 2 else LinkType.group,
                                uses_count=0, is_active=True)
                    session.add(link)
                    await session.commit()
                    return link.token
            token = asyncio.run(create())
            keys = [ec.generate_private_key(ec.SECP256R1()) for _ in range(size)]
            users = []
            for key in keys:
                result = client.post("/api/v1/links/activate", json={
                    "token": token, "public_key": b64(raw_key(key)),
                    "session_credential": secrets.token_urlsafe(32),
                })
                assert result.status_code == 200
                users.append(result.json())
            return client, users, keys
        yield create_room


def encrypted_send(users, keys, size=1048576, message_id="synthetic-file"):
    data = bytes(range(256)) * (size // 256) + bytes(range(size % 256))
    payload = {"v": 1, "type": "attachment", "name": "synthetic-private-name.bin",
               "mime": "application/octet-stream", "size": size, "data": b64(data)}
    plaintext = json.dumps(payload, separators=(",", ":")).encode()
    envelopes = []
    for user, key in zip(users[1:], keys[1:], strict=True):
        ephemeral = ec.generate_private_key(ec.SECP256R1())
        salt, iv = secrets.token_bytes(16), secrets.token_bytes(12)
        context = (f"sideword-web-p256-v1|{users[0]['chat']['id']}|{message_id}|"
                   f"{users[0]['user']['public_id']}|{user['user']['public_id']}").encode()
        material = (keys[0].exchange(ec.ECDH(), key.public_key())
                    + ephemeral.exchange(ec.ECDH(), key.public_key()))
        aes = HKDF(hashes.SHA256(), 32, salt, context).derive(material)
        envelope = {"v": 1, "alg": "P256-2DH-HKDF-SHA256-AES256GCM", "epk": b64(raw_key(ephemeral)),
                    "salt": b64(salt), "iv": b64(iv),
                    "ct": b64(AESGCM(aes).encrypt(iv, plaintext, context))}
        envelopes.append({"recipient_public_id": user["user"]["public_id"],
                          "ciphertext": b64(json.dumps(envelope, separators=(",", ":")).encode())})
    return {"client_message_id": message_id, "envelopes": envelopes}, payload


def decrypt(message, sender, recipient, recipient_id):
    env = json.loads(base64.b64decode(message["ciphertext"]))
    epk = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), base64.b64decode(env["epk"]),
    )
    context = (f"sideword-web-p256-v1|{message['chat_id']}|{message['client_message_id']}|"
               f"{message['sender_public_id']}|{recipient_id}").encode()
    aes = HKDF(hashes.SHA256(), 32, base64.b64decode(env["salt"]), context).derive(
        recipient.exchange(ec.ECDH(), sender.public_key()) + recipient.exchange(ec.ECDH(), epk),
    )
    return json.loads(AESGCM(aes).decrypt(
        base64.b64decode(env["iv"]), base64.b64decode(env["ct"]), context,
    ))


@pytest.mark.parametrize("size,members", [(0, 2), (1048576, 2), (1048576, 3)])
def test_encrypted_files_personal_group_durable_ack_and_idempotent_retries(
    encrypted_room, size, members,
):
    client, users, keys = encrypted_room(members)
    body, payload = encrypted_send(users, keys, size)
    path = f"/api/v1/chats/{users[0]['chat']['id']}/messages"
    accepted = client.post(path, headers=headers(users[0]), json=body)
    assert accepted.status_code == 200
    assert client.post(path, headers=headers(users[0]), json=body).json() == accepted.json()

    async def stored():
        async with SessionLocal() as session:
            return list(await session.scalars(select(PendingMessage).where(
                PendingMessage.chat_id == users[0]["chat"]["id"])))
    rows = asyncio.run(stored())
    assert len(rows) == members - 1
    for row in rows:
        assert payload["name"].encode() not in row.ciphertext
        assert b'application/octet-stream' not in row.ciphertext
        assert b'"data"' not in row.ciphertext
    for index, user in enumerate(users[1:], 1):
        messages = poll(client, user)["messages"]
        assert len(messages) == 1
        assert decrypt(messages[0], keys[0], keys[index], user["user"]["public_id"]) == payload
        assert client.post("/api/v1/ack/exact", headers=headers(user), json={
            "messages": [reference(messages[0])],
        }).json()["deleted_messages"] == 1
        assert poll(client, user)["messages"] == []
    assert client.post(path, headers=headers(users[0]), json=body).json() == accepted.json()
    assert asyncio.run(stored()) == []
    assert poll(client, users[0])["read_receipts"] == []  # Durable delivery is not viewing.


def test_file_batches_bound_poll_and_websocket_backlog_before_loading_ciphertext(encrypted_room):
    client, users, keys = encrypted_room()
    path = f"/api/v1/chats/{users[0]['chat']['id']}/messages"
    for index in range(3):
        body, _ = encrypted_send(users, keys, message_id=f"file-{index}")
        assert client.post(path, headers=headers(users[0]), json=body).status_code == 200
    response = client.get("/api/v1/poll", headers=headers(users[1]))
    assert len(response.content) < 3 * 1024 * 1024
    assert len(response.json()["messages"]) == 1

    async def backlog():
        async with SessionLocal() as session:
            user = await session.scalar(select(User).where(
                User.public_id == users[1]["user"]["public_id"]))
            messages, _, _ = await _backlog_payload(session, user)
            return [item.model_dump(mode="json") for item in messages]
    assert asyncio.run(backlog()) == response.json()["messages"]
    for index in range(3):
        message = poll(client, users[1])["messages"][0]
        assert message["client_message_id"] == f"file-{index}"
        client.post("/api/v1/ack/exact", headers=headers(users[1]),
                    json={"messages": [reference(message)]})
    assert poll(client, users[1])["messages"] == []


def test_real_large_websocket_delivery_stays_ciphertext(encrypted_room):
    client, users, keys = encrypted_room()
    body, payload = encrypted_send(users, keys)
    with client.websocket_connect("/ws", subprotocols=["sideword.v1"]) as ws:
        ws.send_json({"type": "auth", "token": users[1]["token"]})
        assert ws.receive_json()["type"] == "hello"
        result = client.post(f"/api/v1/chats/{users[0]['chat']['id']}/messages",
                             headers=headers(users[0]), json=body)
        assert result.status_code == 200
        event = ws.receive_json()
        assert event["type"] == "message"
        assert decrypt(event["message"], keys[0], keys[1], users[1]["user"]["public_id"]) == payload
        assert len(poll(client, users[1])["messages"]) == 1  # Streaming is not an ACK.


def test_transport_limits_and_atomic_group_capacity(encrypted_room, monkeypatch):
    client, users, keys = encrypted_room(6)
    body, _ = encrypted_send(users, keys)
    path = f"/api/v1/chats/{users[0]['chat']['id']}/messages"
    assert client.post(path, headers=headers(users[0]), json=body).status_code == 413
    assert poll(client, users[1])["messages"] == []
    body, _ = encrypted_send(users, keys, size=16)
    monkeypatch.setattr(get_settings(), "max_pending_bytes_per_chat", 1)
    assert client.post(path, headers=headers(users[0]), json=body).status_code == 429
    assert poll(client, users[-1])["messages"] == []
    monkeypatch.setattr(get_settings(), "max_pending_bytes_per_chat", 32 * 1024 * 1024)
    monkeypatch.setattr(get_settings(), "max_pending_per_chat", 4)
    assert client.post(path, headers=headers(users[0]), json=body).status_code == 429
    monkeypatch.setattr(get_settings(), "max_pending_per_chat", 2000)
    body["envelopes"] *= 21
    assert client.post(path, headers=headers(users[0]), json=body).status_code == 422
    body["envelopes"] = body["envelopes"][:1]
    body["envelopes"][0]["ciphertext"] = b64(b'x' * (2 * 1024 * 1024 + 1))
    assert client.post(path, headers=headers(users[0]), json=body).status_code in {413, 422}


def test_file_authorization_closure_and_expiry(encrypted_room):
    client, users, keys = encrypted_room()
    _, foreign, _ = encrypted_room()
    body, _ = encrypted_send(users, keys, size=32)
    chat_id = users[0]["chat"]["id"]
    path = f"/api/v1/chats/{chat_id}/messages"
    assert client.post(path, headers=headers(foreign[0]), json=body).status_code == 404
    assert client.post(path, headers=headers(users[0]), json=body).status_code == 200
    delivery = poll(client, users[1])["messages"][0]
    assert client.post("/api/v1/ack/exact", headers=headers(foreign[0]), json={
        "messages": [reference(delivery)],
    }).json()["deleted_messages"] == 0

    async def close():
        async with SessionLocal() as session:
            chat = await session.get(Chat, chat_id)
            chat.closed_at = datetime.now(timezone.utc)
            await session.commit()
    asyncio.run(close())
    assert poll(client, users[1])["messages"] == []
    assert client.post(path, headers=headers(users[0]), json=body).status_code == 410

    async def expire():
        from app.cleanup import _purge_once
        async with SessionLocal() as session:
            row = await session.scalar(select(PendingMessage).where(
                PendingMessage.chat_id == chat_id))
            row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await session.commit()
        await _purge_once()
    asyncio.run(expire())
    assert client.post("/api/v1/ack/exact", headers=headers(users[1]), json={
        "messages": [reference(delivery)],
    }).json()["deleted_messages"] == 0


def test_chunked_and_lying_length_rejected_without_reading_remainder(monkeypatch):
    monkeypatch.setattr(get_settings(), "max_send_request_bytes", 1024)

    async def scenario(length):
        reached, sent, reads = [], [], []
        async def app(scope, receive, send):
            reached.append(True)
        async def receive():
            reads.append(True)
            return {"type": "http.request", "body": b'x' * 600, "more_body": True}
        async def send(message):
            sent.append(message)
        scope = {"type": "http", "method": "POST", "path": "/api/v1/chats/1/messages",
                 "scheme": "http", "client": ("127.0.0.1", 1), "server": ("testserver", 80),
                 "headers": [] if length is None else [(b'content-length', length)]}
        guard = SecurityGuards(app)
        await guard(scope, receive, send)
        assert not reached
        assert len(reads) == 2
        assert sent[0]["status"] == 413
        assert guard.http_requests == 0
    for length in [None, b'1']:
        asyncio.run(scenario(length))


def test_concurrent_http_work_rejected_before_body_read(monkeypatch):
    monkeypatch.setattr(get_settings(), "max_http_concurrency", 1)

    async def scenario():
        started, finish = asyncio.Event(), asyncio.Event()
        async def app(scope, receive, send):
            started.set()
            await finish.wait()
        async def receive():
            return {"type": "http.request", "body": b'', "more_body": False}
        async def discard(message):
            pass
        scope = {"type": "http", "method": "GET", "path": "/api/v1/poll", "scheme": "http",
                 "client": ("127.0.0.1", 1), "server": ("testserver", 80), "headers": []}
        guard = SecurityGuards(app)
        task = asyncio.create_task(guard(scope, receive, discard))
        await started.wait()
        async def forbidden_receive():
            raise AssertionError("overloaded request body must not be read")
        sent = []
        async def send(message):
            sent.append(message)
        await guard(scope, forbidden_receive, send)
        assert sent[0]["status"] == 429
        # Saturated API work must not prevent scripts/fonts from loading.
        asset_entered = asyncio.Event()
        original_app = guard.app
        async def asset_app(scope, receive, send):
            asset_entered.set()
            await finish.wait()
        guard.app = asset_app
        asset = asyncio.create_task(guard({**scope, "path": "/static/client.js"}, receive, send))
        await asyncio.wait_for(asset_entered.wait(), timeout=1)
        assert guard.static_requests == 1
        guard.app = original_app
        finish.set()
        await asyncio.gather(task, asset)
        assert guard.http_requests == guard.static_requests == 0
    asyncio.run(scenario())


def test_concurrent_live_push_has_no_growing_socket_queue():
    async def scenario():
        manager = ConnectionManager()
        started, finish = asyncio.Event(), asyncio.Event()
        calls = []
        class Socket:
            async def send_json(self, payload):
                calls.append(payload)
                started.set()
                await finish.wait()
        socket = Socket()
        async def valid():
            return True
        await manager.connect(1, socket, valid, public_id="synthetic")
        task = asyncio.create_task(manager.send_json(1, {"type": "synthetic"}))
        await started.wait()
        await asyncio.gather(*(manager.send_json(1, {"type": "synthetic"}) for _ in range(20)))
        assert len(calls) == 1
        finish.set()
        await task
        assert not manager._sending
    asyncio.run(scenario())


def test_delivery_batch_cannot_load_a_reused_row_id_outside_its_budget(room):
    client, users = room()
    from test_exact_delivery import send
    send(client, users)

    async def replace_between_queries():
        async with SessionLocal() as session:
            user = await session.scalar(select(User).where(
                User.public_id == users[1]["user"]["public_id"]))
            original = await session.scalar(select(PendingMessage).where(
                PendingMessage.recipient_id == user.id))
            original_id = original.id
            calls = 0

            class RacingSession:
                async def execute(self, query):
                    nonlocal calls
                    calls += 1
                    result = await session.execute(query)
                    if calls == 1:
                        await session.execute(delete(PendingMessage).where(
                            PendingMessage.id == original_id))
                        session.add(PendingMessage(
                            id=original_id, chat_id=original.chat_id, sender_id=original.sender_id,
                            recipient_id=user.id, client_message_id="replacement",
                            ciphertext=b'x' * (2 * 1024 * 1024),
                        ))
                        await session.commit()
                    return result

            assert await queued_messages(RacingSession(), user.id) == []
            assert (await queued_messages(session, user.id))[0].client_message_id == "replacement"
    asyncio.run(replace_between_queries())
