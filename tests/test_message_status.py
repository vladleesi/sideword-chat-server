"""Distinct durable delivery/viewing, original recipients and exact authorization."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select
from test_exact_delivery import headers, poll, reference, send
from test_exact_delivery import room as _room

from app.db import SessionLocal
from app.models import Chat, ChatMember, ChatType, ReadReceipt, SendRecord, User

room = _room


def statuses(client, user, message_id="same-id", chat_id=None):
    response = client.post(
        f"/api/v1/chats/{chat_id or user['chat']['id']}/messages/status",
        headers=headers(user),
        json={"client_message_ids": [message_id]},
    )
    return response


def delivered(client, user, message):
    return client.post(
        "/api/v1/ack/exact",
        headers=headers(user),
        json={
            "confirm_delivery": True,
            "messages": [reference(message)],
        },
    )


def viewed(client, user, message):
    return client.post(
        f"/api/v1/chats/{message['chat_id']}/read/exact",
        headers=headers(user),
        json={"viewed": True, "messages": [reference(message)]},
    )


@pytest.mark.parametrize("personal", [False, True])
def test_durable_delivery_does_not_mean_read_and_ciphertext_is_removed(room, personal):
    client, users = room()
    if personal:

        async def change():
            async with SessionLocal() as session:
                chat = await session.get(Chat, users[0]["chat"]["id"])
                chat.chat_type = ChatType.personal
                await session.commit()

        asyncio.run(change())
    send(client, users)
    message = poll(client, users[1])["messages"][0]
    initial = statuses(client, users[0]).json()["statuses"][0]
    assert initial["recipients"][0]["delivered_at"] is None  # Streaming isn't durable delivery.
    assert viewed(client, users[1], message).json() == {"marked": 0}
    assert delivered(client, users[1], message).json()["deleted_messages"] == 1
    assert poll(client, users[1])["messages"] == []
    receipts = poll(client, users[0])
    assert receipts["read_receipts"] == []
    delivery = receipts["delivery_receipts"][0]
    assert delivery["message_delivery_id"] == message["delivery_id"]
    assert delivery["view_confirmed"] is False
    state = statuses(client, users[0]).json()["statuses"][0]["recipients"][0]
    assert state["delivered_at"] and state["read_at"] is None
    assert viewed(client, users[1], message).json() == {"marked": 1}
    state_after = statuses(client, users[0]).json()["statuses"][0]["recipients"][0]
    assert state_after["delivered_at"] == state["delivered_at"]
    assert state_after["read_at"] >= state_after["delivered_at"]
    assert viewed(client, users[1], message).json() == {"marked": 0}
    assert delivered(client, users[1], message).json()["deleted_messages"] == 0
    receipts = poll(client, users[0])
    assert len(receipts["delivery_receipts"]) == len(receipts["read_receipts"]) == 1
    assert receipts["read_receipts"][0]["view_confirmed"] is True
    for receipt in receipts["delivery_receipts"] + receipts["read_receipts"]:
        response = client.post(
            "/api/v1/ack/exact",
            headers=headers(users[0]),
            json={"receipts": [reference(receipt, receipt=True)]},
        )
        assert response.json()["deleted_receipts"] == 1
    assert poll(client, users[0])["read_receipts"] == []
    assert statuses(client, users[0]).json()["statuses"][0]["recipients"][0] == state_after


def test_group_original_recipients_partial_progress_and_authorization(room):
    client, users = room(3)
    outsider_client, outsiders = room()
    send(client, users)
    one, two = [poll(client, user)["messages"][0] for user in users[1:]]
    assert delivered(client, users[1], one).status_code == 200
    assert viewed(client, users[1], one).json()["marked"] == 1
    state = statuses(client, users[0]).json()["statuses"][0]
    assert {peer["public_id"] for peer in state["recipients"]} == {
        user["user"]["public_id"] for user in users[1:]
    }
    assert sum(peer["read_at"] is not None for peer in state["recipients"]) == 1
    assert sum(peer["delivered_at"] is not None for peer in state["recipients"]) == 1
    assert statuses(client, users[1]).json() == {"statuses": []}
    assert statuses(outsider_client, outsiders[0], chat_id=one["chat_id"]).status_code == 404
    assert viewed(client, users[2], one).json() == {"marked": 0}
    assert delivered(client, users[2], one).json()["deleted_messages"] == 0
    assert viewed(outsider_client, outsiders[0], one).status_code == 404
    assert delivered(client, users[2], two).json()["deleted_messages"] == 1
    assert viewed(client, users[2], two).json()["marked"] == 1

    async def remove_member():
        async with SessionLocal() as session:
            await session.execute(
                delete(ChatMember).where(
                    ChatMember.chat_id == one["chat_id"],
                    ChatMember.user_id.in_(
                        select(User.id).where(User.public_id == users[2]["user"]["public_id"])
                    ),
                )
            )
            await session.commit()

    asyncio.run(remove_member())
    final = statuses(client, users[0]).json()["statuses"][0]["recipients"]
    assert len(final) == 2 and all(peer["read_at"] for peer in final)
    assert statuses(client, users[2]).status_code == 404
    assert viewed(client, users[2], two).status_code == 404
    assert poll(client, users[1])["read_receipts"] == []
    assert "ciphertext" not in json.dumps(final) and "display_name" not in json.dumps(final)


@pytest.mark.parametrize(
    "changed,value",
    [("sender_public_id", "wrong"), ("client_message_id", "wrong"), ("delivery_id", "0" * 32)],
)
def test_wrong_original_identity_does_not_confirm_view(room, changed, value):
    client, users = room()
    send(client, users)
    message = poll(client, users[1])["messages"][0]
    delivered(client, users[1], message)
    altered = {**message, changed: value}
    assert viewed(client, users[1], altered).json() == {"marked": 0}
    assert statuses(client, users[0]).json()["statuses"][0]["recipients"][0]["read_at"] is None


def test_colliding_sender_ids_and_concurrent_tabs_are_idempotent(room):
    client, users = room(3)
    send(client, users, sender=0)
    send(client, users, sender=1)
    messages = poll(client, users[2])["messages"]
    for message in messages:
        with ThreadPoolExecutor(2) as pool:
            responses = list(
                pool.map(lambda _, item=message: delivered(client, users[2], item), range(2))
            )
        assert sum(response.json()["deleted_messages"] for response in responses) == 1
        with ThreadPoolExecutor(2) as pool:
            responses = list(
                pool.map(lambda _, item=message: viewed(client, users[2], item), range(2))
            )
        assert sum(response.json()["marked"] for response in responses) == 1
    for sender in users[:2]:
        receipts = poll(client, sender)
        assert len(receipts["delivery_receipts"]) == len(receipts["read_receipts"]) == 1
        own = statuses(client, sender).json()["statuses"][0]
        assert sum(peer["read_at"] is not None for peer in own["recipients"]) == 1


def test_legacy_read_confirms_delivery_without_claiming_viewing(room):
    client, users = room()
    send(client, users)
    message = poll(client, users[1])["messages"][0]
    response = client.post(
        f"/api/v1/chats/{message['chat_id']}/read/exact",
        headers=headers(users[1]),
        json={"messages": [reference(message)]},
    )
    assert response.json()["marked"] == 1
    assert poll(client, users[0])["read_receipts"][0]["view_confirmed"] is False
    state = statuses(client, users[0]).json()["statuses"][0]["recipients"][0]
    assert state["delivered_at"] and state["read_at"] is None


def test_full_receipt_queue_never_delays_durable_ack_and_status_recovers(room, monkeypatch):
    from app.routers import chats as routes

    client, users = room()
    monkeypatch.setattr(routes._settings, "max_read_receipts", 0)
    # Send first under an available queue; quota applies at ACK time.
    monkeypatch.setattr(routes._settings, "max_read_receipts", 100)
    send(client, users)
    message = poll(client, users[1])["messages"][0]
    monkeypatch.setattr(routes._settings, "max_read_receipts", 0)
    assert delivered(client, users[1], message).json()["deleted_messages"] == 1
    assert viewed(client, users[1], message).json()["marked"] == 1
    assert poll(client, users[1])["messages"] == []
    assert poll(client, users[0])["delivery_receipts"] == []
    assert poll(client, users[0])["read_receipts"] == []
    assert statuses(client, users[0]).json()["statuses"][0]["recipients"][0]["read_at"]


def test_metadata_expiry_retention_and_no_stale_confirmations(room):
    from app.cleanup import _purge_once
    from app.config import get_settings

    client, users = room()
    send(client, users)
    message = poll(client, users[1])["messages"][0]
    delivered(client, users[1], message)

    async def age(days):
        async with SessionLocal() as session:
            record = await session.scalar(
                select(SendRecord).where(SendRecord.chat_id == users[0]["chat"]["id"])
            )
            record.created_at = datetime.now(timezone.utc) - timedelta(days=days)
            record.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await session.commit()

    asyncio.run(age(1))
    asyncio.run(_purge_once())
    assert statuses(client, users[0]).json()["statuses"]
    asyncio.run(age(get_settings().message_ttl_days + 1))
    assert statuses(client, users[0]).json() == {"statuses": []}
    assert viewed(client, users[1], message).json() == {"marked": 0}
    asyncio.run(_purge_once())

    async def count():
        async with SessionLocal() as session:
            assert (
                await session.scalar(
                    select(SendRecord).where(SendRecord.chat_id == users[0]["chat"]["id"])
                )
                is None
            )

    asyncio.run(count())


def test_receipt_backlog_queues_are_independent_for_legacy_clients(room):
    client, users = room()

    async def seed():
        async with SessionLocal() as session:
            for index in range(101):
                session.add(
                    ReadReceipt(
                        chat_id=users[0]["chat"]["id"],
                        sender_id=await session.scalar(
                            select(User.id).where(User.public_id == users[0]["user"]["public_id"])
                        ),
                        reader_public_id=users[1]["user"]["public_id"],
                        client_message_id=str(index),
                        kind="delivered",
                    )
                )
            session.add(
                ReadReceipt(
                    chat_id=users[0]["chat"]["id"],
                    sender_id=await session.scalar(
                        select(User.id).where(User.public_id == users[0]["user"]["public_id"])
                    ),
                    reader_public_id=users[1]["user"]["public_id"],
                    client_message_id="viewed",
                    kind="viewed",
                )
            )
            await session.commit()

    asyncio.run(seed())
    backlog = poll(client, users[0])
    assert len(backlog["delivery_receipts"]) == 100
    assert len(backlog["read_receipts"]) == 1

    async def remove():
        async with SessionLocal() as session:
            await session.execute(
                delete(ChatMember).where(
                    ChatMember.user_id.in_(
                        select(User.id).where(User.public_id == users[0]["user"]["public_id"])
                    )
                )
            )
            await session.commit()

    asyncio.run(remove())
    assert (
        poll(client, users[0])["delivery_receipts"] == poll(client, users[0])["read_receipts"] == []
    )


def test_live_websocket_and_reconnect_backlog_use_the_same_confirmation_identity(room):
    client, users = room()
    send(client, users)
    message = poll(client, users[1])["messages"][0]
    with client.websocket_connect("/ws", subprotocols=["sideword.v1"]) as socket:
        socket.send_json({"type": "auth", "token": users[0]["token"]})
        assert socket.receive_json()["type"] == "hello"
        delivered(client, users[1], message)
        delivery = socket.receive_json()
        assert delivery["type"] == "delivered"
        assert delivery["delivery"]["message_delivery_id"] == message["delivery_id"]
        viewed(client, users[1], message)
        read = socket.receive_json()
        assert read["type"] == "read" and read["read"]["view_confirmed"] is True
    with client.websocket_connect("/ws", subprotocols=["sideword.v1"]) as socket:
        socket.send_json({"type": "auth", "token": users[0]["token"]})
        backlog = socket.receive_json()["backlog"]
        assert backlog["delivery_receipts"] == [delivery["delivery"]]
        assert backlog["read_receipts"] == [read["read"]]


def test_old_read_reference_cannot_confirm_reused_logical_message(room):
    client, users = room()
    send(client, users)
    old = poll(client, users[1])["messages"][0]
    delivered(client, users[1], old)

    async def expire_retry():
        async with SessionLocal() as session:
            record = await session.scalar(
                select(SendRecord).where(SendRecord.chat_id == users[0]["chat"]["id"])
            )
            record.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await session.commit()

    asyncio.run(expire_retry())
    send(client, users)
    new = poll(client, users[1])["messages"][0]
    assert old["delivery_id"] != new["delivery_id"]
    delivered(client, users[1], new)
    assert viewed(client, users[1], old).json() == {"marked": 0}
    assert viewed(client, users[1], new).json() == {"marked": 1}


def test_status_requests_are_bounded_and_require_a_valid_session(room):
    client, users = room()
    route = f"/api/v1/chats/{users[0]['chat']['id']}/messages/status"
    assert client.post(route, json={"client_message_ids": ["id"]}).status_code == 401
    for body in [
        {"client_message_ids": []},
        {"client_message_ids": ["x"] * 101},
        {"client_message_ids": ["x" * 65]},
        {"client_message_ids": ["x"], "other": True},
    ]:
        assert client.post(route, headers=headers(users[0]), json=body).status_code == 422
    assert statuses(client, users[0], "missing").json() == {"statuses": []}


def test_delivery_state_write_failure_rolls_back_ciphertext_deletion(room, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    from app.routers import chats as routes

    client, users = room()
    send(client, users)
    message = poll(client, users[1])["messages"][0]
    original = routes._record_delivery

    async def fail(*args):
        raise SQLAlchemyError("synthetic write failure")

    monkeypatch.setattr(routes, "_record_delivery", fail)
    with pytest.raises(SQLAlchemyError):
        delivered(client, users[1], message)
    monkeypatch.setattr(routes, "_record_delivery", original)
    assert poll(client, users[1])["messages"][0]["delivery_id"] == message["delivery_id"]
    assert statuses(client, users[0]).json()["statuses"][0]["recipients"][0]["delivered_at"] is None
    assert delivered(client, users[1], message).json()["deleted_messages"] == 1
