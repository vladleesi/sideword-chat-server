"""Exact delivery acknowledgements use isolated test databases and credentials."""

import asyncio
import base64
import secrets
import shutil
from concurrent.futures import ThreadPoolExecutor

import pytest
from csrf_client import TestClient
from message_key_fixtures import public_key
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import SessionLocal
from app.main import create_app
from app.models import Link, LinkType


@pytest.fixture
def room():
    with TestClient(create_app()) as client:
        def create_room(size=2):
            async def create():
                async with SessionLocal() as session:
                    link = Link(token=secrets.token_urlsafe(32), link_type=LinkType.group,
                                max_uses=size, uses_count=0, is_active=True)
                    session.add(link)
                    await session.commit()
                    return link.token
            token = asyncio.run(create())
            users = []
            for _ in range(size):
                response = client.post("/api/v1/links/activate", json={
                    "token": token,
                    "public_key": base64.b64encode(public_key()).decode(),
                })
                assert response.status_code == 200
                users.append(response.json())
            return client, users
        yield create_room


def headers(user):
    return {"Authorization": f"Bearer {user['token']}"}


def send(client, users, sender=0, message_id="same-id"):
    response = client.post(f"/api/v1/chats/{users[0]['chat']['id']}/messages", json={
        "client_message_id": message_id,
        "envelopes": [{"recipient_public_id": user["user"]["public_id"], "ciphertext": "eA=="}
                      for i, user in enumerate(users) if i != sender],
    }, headers=headers(users[sender]))
    assert response.status_code == 200


def poll(client, user):
    response = client.get("/api/v1/poll", headers=headers(user))
    assert response.status_code == 200
    return response.json()


def reference(delivery, receipt=False):
    fields = ("delivery_id", "chat_id", "client_message_id",
              "reader_public_id" if receipt else "sender_public_id")
    return {field: delivery[field] for field in fields}


def read(client, user, ref):
    return client.post(f"/api/v1/chats/{user['chat']['id']}/read/exact",
                       headers=headers(user), json={"messages": [ref]})


def test_group_collision_only_deletes_authenticated_sender_delivery(room):
    client, users = room(3)
    send(client, users, sender=0)
    send(client, users, sender=1)
    messages = poll(client, users[2])["messages"]
    assert len(messages) == 2
    assert read(client, users[2], reference(messages[0])).json() == {"marked": 1}
    remaining = poll(client, users[2])["messages"]
    assert [m["delivery_id"] for m in remaining] == [messages[1]["delivery_id"]]
    assert len(poll(client, users[0])["read_receipts"]) == 1
    assert poll(client, users[1])["read_receipts"] == []


def test_delayed_read_and_ack_cannot_consume_reused_row_or_logical_id(room):
    client, users = room()
    send(client, users)
    old = poll(client, users[1])["messages"][0]
    assert read(client, users[1], reference(old)).json() == {"marked": 1}
    send(client, users, message_id="next-message")  # New logical delivery reuses the row ID.
    new = poll(client, users[1])["messages"][0]
    assert old["id"] == new["id"]
    assert old["delivery_id"] != new["delivery_id"]
    assert read(client, users[1], reference(old)).json() == {"marked": 0}
    response = client.post("/api/v1/ack/exact", headers=headers(users[1]),
                           json={"messages": [reference(old)]})
    assert response.json() == {"deleted_messages": 0, "deleted_receipts": 0}
    assert poll(client, users[1])["messages"] == [new]
    response = client.post("/api/v1/ack/exact", headers=headers(users[1]),
                           json={"messages": [reference(new)]})
    assert response.json()["deleted_messages"] == 1


def test_delayed_receipt_ack_does_not_delete_reused_receipt_id(room):
    client, users = room()
    send(client, users)
    read(client, users[1], reference(poll(client, users[1])["messages"][0]))
    old = poll(client, users[0])["read_receipts"][0]
    body = {"receipts": [reference(old, receipt=True)]}
    assert client.post("/api/v1/ack/exact", headers=headers(users[0]),
                       json=body).json()["deleted_receipts"] == 1
    send(client, users, message_id="next-message")
    read(client, users[1], reference(poll(client, users[1])["messages"][0]))
    new = poll(client, users[0])["read_receipts"][0]
    assert old["id"] == new["id"]
    assert old["delivery_id"] != new["delivery_id"]
    assert client.post("/api/v1/ack/exact", headers=headers(users[0]),
                       json=body).json()["deleted_receipts"] == 0
    assert poll(client, users[0])["read_receipts"] == [new]


@pytest.mark.parametrize("field,value", [
    ("delivery_id", "0" * 32), ("client_message_id", "wrong"),
    ("sender_public_id", "wrong"), ("chat_id", 2147483647),
])
def test_exact_identity_and_owner_are_required(room, field, value):
    client, users = room()
    send(client, users)
    message = poll(client, users[1])["messages"][0]
    ref = reference(message)
    wrong = {**ref, field: value}
    response = read(client, users[1], wrong)
    assert response.status_code == 422 if field == "chat_id" else response.json() == {"marked": 0}
    for user, item in [(users[1], wrong), (users[0], ref)]:
        response = client.post("/api/v1/ack/exact", headers=headers(user),
                               json={"messages": [item]})
        assert response.json()["deleted_messages"] == 0
    assert poll(client, users[1])["messages"] == [message]


def test_concurrent_reads_and_lost_response_retries_create_one_receipt(room):
    client, users = room()
    send(client, users)
    ref = reference(poll(client, users[1])["messages"][0])
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: read(client, users[1], ref), range(2)))
    assert sorted(response.json()["marked"] for response in responses) == [0, 1]
    assert read(client, users[1], ref).json() == {"marked": 0}
    assert len(poll(client, users[0])["read_receipts"]) == 1


def test_exact_schema_rejects_legacy_fields_and_unbounded_batches(room):
    client, users = room()
    send(client, users)
    ref = reference(poll(client, users[1])["messages"][0])
    for body in ({"message_ids": [1]}, {"messages": [ref] * 101},
                 {"messages": [{**ref, "delivery_id": ""}]}):
        assert client.post("/api/v1/ack/exact", headers=headers(users[1]),
                           json=body).status_code == 422
    assert len(poll(client, users[1])["messages"]) == 1


def test_receipt_identity_and_owner_are_required(room):
    client, users = room()
    send(client, users)
    read(client, users[1], reference(poll(client, users[1])["messages"][0]))
    receipt = poll(client, users[0])["read_receipts"][0]
    ref = reference(receipt, receipt=True)
    for field, value in (("delivery_id", "0" * 32), ("reader_public_id", "wrong"),
                         ("client_message_id", "wrong"), ("chat_id", 2147483647)):
        response = client.post("/api/v1/ack/exact", headers=headers(users[0]),
                               json={"receipts": [{**ref, field: value}]})
        assert response.json()["deleted_receipts"] == 0
    response = client.post("/api/v1/ack/exact", headers=headers(users[1]),
                           json={"receipts": [ref]})
    assert response.json()["deleted_receipts"] == 0
    assert poll(client, users[0])["read_receipts"] == [receipt]


def test_receipt_write_failure_rolls_back_message_deletion(room):
    from app.db import engine

    client, users = room()
    send(client, users)
    message = poll(client, users[1])["messages"][0]

    def fail_receipt(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO read_receipts"):
            raise RuntimeError("simulated storage failure")

    event.listen(engine.sync_engine, "before_cursor_execute", fail_receipt)
    try:
        with pytest.raises(RuntimeError, match="simulated storage failure"):
            read(client, users[1], reference(message))
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", fail_receipt)
    assert poll(client, users[1])["messages"] == [message]
    assert poll(client, users[0])["read_receipts"] == []


def test_ws_live_backlog_and_poll_share_delivery_references(room):
    client, users = room()
    send(client, users, message_id="backlog")
    polled = poll(client, users[1])["messages"][0]
    with client.websocket_connect("/ws") as receiver:
        receiver.send_json({"type": "auth", "token": users[1]["token"]})
        hello = receiver.receive_json()
        assert reference(hello["backlog"]["messages"][0]) == reference(polled)
        send(client, users, message_id="live")
        live = receiver.receive_json()["message"]
        assert reference(poll(client, users[1])["messages"][-1]) == reference(live)
        with client.websocket_connect("/ws") as sender:
            sender.send_json({"type": "auth", "token": users[0]["token"]})
            sender.receive_json()
            assert read(client, users[1], reference(live)).json() == {"marked": 1}
            receipt = sender.receive_json()["read"]
            assert reference(poll(client, users[0])["read_receipts"][0], True) == reference(
                receipt, True
            )
    with client.websocket_connect("/ws") as sender:
        sender.send_json({"type": "auth", "token": users[0]["token"]})
        assert reference(sender.receive_json()["backlog"]["read_receipts"][0], True) == reference(
            receipt, True
        )


def test_legacy_queue_migration_and_backup_preserve_delivery_ids(tmp_path, monkeypatch):
    import app.db as db

    async def exercise():
        original = tmp_path / "original.sqlite3"
        backup = tmp_path / "backup.sqlite3"
        engine = create_async_engine(f"sqlite+aiosqlite:///{original}")
        monkeypatch.setattr(db, "engine", engine)
        async with engine.begin() as conn:
            for table in ("pending_messages", "read_receipts"):
                await conn.execute(text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)"))
                await conn.execute(text(f"INSERT INTO {table} (id) VALUES (1), (2)"))
        await db.init_db()

        async with db.engine.connect() as conn:
            receipt_columns = {row[1] for row in await conn.execute(text(
                "PRAGMA table_info(read_receipts)"))}
            ledger_columns = {row[1] for row in await conn.execute(text(
                "PRAGMA table_info(send_records)"))}
            assert "message_delivery_id" in receipt_columns
            assert "receipt_state_json" in ledger_columns
            assert list((await conn.execute(text(
                "SELECT message_delivery_id FROM read_receipts"))).scalars()) == [None, None]

        async def identifiers():
            async with db.engine.connect() as conn:
                return [list((await conn.execute(text(
                    f"SELECT id, delivery_id FROM {table} ORDER BY id"
                ))).all()) for table in ("pending_messages", "read_receipts")]

        before = await identifiers()
        assert len({delivery for rows in before for _, delivery in rows}) == 4
        assert all(len(delivery) == 32 for rows in before for _, delivery in rows)
        await db.init_db()
        assert await identifiers() == before
        await engine.dispose()
        shutil.copyfile(original, backup)  # Closed, isolated DB: no WAL or running server.
        restored = create_async_engine(f"sqlite+aiosqlite:///{backup}")
        monkeypatch.setattr(db, "engine", restored)
        try:
            await db.init_db()
            assert await identifiers() == before
        finally:
            await restored.dispose()

    asyncio.run(exercise())
