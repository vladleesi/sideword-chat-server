"""Host aliases share authenticated rooms, queues and durable confirmations."""

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from test_exact_delivery import headers, reference
from test_exact_delivery import room as _room

from app.db import SessionLocal
from app.models import ClientSession

room = _room
ORIGINS = ("https://one.example.test", "https://two.example.test")


@contextmanager
def connection(client, origin, user):
    with client.websocket_connect(
        origin.replace("https:", "wss:") + "/ws",
        subprotocols=["sideword.v1"], headers={"Origin": origin},
    ) as socket:
        socket.send_json({"type": "auth", "token": user["token"]})
        hello = socket.receive_json()
        assert hello["type"] == "hello"
        assert hello["user"] == user["user"]["public_id"]
        yield socket, hello["backlog"]


def submit(client, origin, users, sender, message_id):
    response = client.post(
        f"{origin}/api/v1/chats/{users[0]['chat']['id']}/messages",
        headers=headers(users[sender]),
        json={
            "client_message_id": message_id,
            "envelopes": [
                {"recipient_public_id": user["user"]["public_id"], "ciphertext": "eA=="}
                for index, user in enumerate(users) if index != sender
            ],
        },
    )
    assert response.status_code == 200
    return response.json()


def confirm(client, origin, user, messages):
    response = client.post(
        origin + "/api/v1/ack/exact", headers=headers(user),
        json={"confirm_delivery": True, "messages": [reference(item) for item in messages]},
    )
    assert response.status_code == 200
    return response.json()["deleted_messages"]


def status(client, origin, user, message_id):
    response = client.post(
        f"{origin}/api/v1/chats/{user['chat']['id']}/messages/status",
        headers=headers(user), json={"client_message_ids": [message_id]},
    )
    assert response.status_code == 200
    return response.json()["statuses"][0]


@pytest.mark.parametrize("origins", [
    (ORIGINS[0], ORIGINS[0]), (ORIGINS[1], ORIGINS[1]), ORIGINS,
])
def test_bidirectional_delivery_across_host_aliases_requires_remote_confirmation(room, origins):
    client, users = room()
    with (
        connection(client, origins[0], users[0]) as (first, first_backlog),
        connection(client, origins[1], users[1]) as (second, second_backlog),
    ):
        assert first_backlog["messages"] == second_backlog["messages"] == []
        sockets = (first, second)
        for sender in (0, 1):
            recipient = 1 - sender
            message_id = f"direction-{sender}"
            acceptance = submit(client, origins[sender], users, sender, message_id)
            event = sockets[recipient].receive_json()
            assert event["type"] == "message"
            message = event["message"]
            assert message["chat_id"] == users[sender]["chat"]["id"]
            assert message["sender_public_id"] == users[sender]["user"]["public_id"]
            assert message["delivery_id"] == acceptance["deliveries"][
                users[recipient]["user"]["public_id"]
            ]
            progress = status(client, origins[sender], users[sender], message_id)
            assert progress["recipients"][0]["delivered_at"] is None
            assert confirm(client, origins[recipient], users[recipient], [message]) == 1
            receipt = sockets[sender].receive_json()
            assert receipt["type"] == "delivered"
            assert receipt["delivery"]["message_delivery_id"] == message["delivery_id"]
            progress = status(client, origins[sender], users[sender], message_id)
            assert progress["recipients"][0]["delivered_at"] is not None
            assert progress["recipients"][0]["read_at"] is None


def test_distinct_device_identity_on_another_origin_reaches_existing_room_members(room):
    client, users = room(3)
    assert len({user["user"]["public_id"] for user in users}) == 3
    assert len({user["chat"]["id"] for user in users}) == 1
    origins = (ORIGINS[0], ORIGINS[0], ORIGINS[1])
    with (
        connection(client, ORIGINS[0], users[0]) as (first, _),
        connection(client, ORIGINS[0], users[1]) as (second, _),
        connection(client, ORIGINS[1], users[2]) as (third, _),
    ):
        sockets = (first, second, third)
        for sender in (2, 0):
            acceptance = submit(client, origins[sender], users, sender, f"device-{sender}")
            for recipient in (index for index in range(3) if index != sender):
                event = sockets[recipient].receive_json()
                assert event["type"] == "message"
                message = event["message"]
                assert message["delivery_id"] == acceptance["deliveries"][
                    users[recipient]["user"]["public_id"]
                ]
                assert confirm(client, origins[recipient], users[recipient], [message]) == 1
                assert sockets[sender].receive_json()["type"] == "delivered"


@pytest.mark.parametrize("initial_origin", ORIGINS)
@pytest.mark.parametrize("switch_origin", [False, True])
def test_refresh_and_domain_switch_recover_unconfirmed_delivery_without_duplicate_queue(
    room, initial_origin, switch_origin,
):
    client, users = room()
    sender_origin = ORIGINS[0]
    reconnect_origin = (
        next(origin for origin in ORIGINS if origin != initial_origin)
        if switch_origin else initial_origin
    )
    with connection(client, sender_origin, users[0]) as (sender, _):
        with connection(client, initial_origin, users[1]) as (recipient, _):
            acceptance = submit(client, sender_origin, users, 0, "interrupted")
            streamed = recipient.receive_json()["message"]
            # A streamed/local-rendered message is still queued until durable ACK.
            assert status(client, sender_origin, users[0], "interrupted")[
                "recipients"
            ][0]["delivered_at"] is None
        retry = submit(client, sender_origin, users, 0, "interrupted")
        for field in ("client_message_id", "recipients", "deliveries"):
            assert retry[field] == acceptance[field]
        submit(client, sender_origin, users, 0, "offline")
        with connection(client, reconnect_origin, users[1]) as (_, backlog):
            messages = backlog["messages"]
            assert [item["client_message_id"] for item in messages] == ["interrupted", "offline"]
            assert messages[0]["delivery_id"] == streamed["delivery_id"]
            assert confirm(client, reconnect_origin, users[1], messages) == 2
            assert confirm(client, reconnect_origin, users[1], messages) == 0
            receipts = [sender.receive_json(), sender.receive_json()]
            assert {item["delivery"]["message_delivery_id"] for item in receipts} == {
                item["delivery_id"] for item in messages
            }
        with connection(client, reconnect_origin, users[1]) as (_, backlog):
            assert backlog["messages"] == []


@pytest.mark.parametrize("origin", ORIGINS)
def test_alias_does_not_authorize_unrelated_identity_or_acknowledgements(room, origin):
    client, users = room()
    _, unrelated = room()
    submit(client, ORIGINS[0], users, 0, "private-room")
    queued = client.get(ORIGINS[0] + "/api/v1/poll", headers=headers(users[1])).json()
    with connection(client, origin, unrelated[0]) as (_, backlog):
        assert backlog["messages"] == []
        assert confirm(client, origin, unrelated[0], queued["messages"]) == 0
        chat_id = users[0]["chat"]["id"]
        assert client.get(
            f"{origin}/api/v1/chats/{chat_id}", headers=headers(unrelated[0]),
        ).status_code == 404
        assert client.post(
            f"{origin}/api/v1/chats/{chat_id}/messages/status",
            headers=headers(unrelated[0]), json={"client_message_ids": ["private-room"]},
        ).status_code == 404
    assert confirm(client, ORIGINS[1], users[1], queued["messages"]) == 1


@pytest.mark.parametrize("origin", ORIGINS)
def test_expired_session_cannot_reconnect_through_another_alias(room, origin):
    client, users = room()

    async def expire():
        async with SessionLocal() as session:
            record = await session.get(ClientSession, users[0]["session_id"])
            record.expires_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
            await session.commit()

    asyncio.run(expire())
    assert client.get(origin + "/api/v1/me", headers=headers(users[0])).status_code == 401
    with client.websocket_connect(
        origin.replace("https:", "wss:") + "/ws",
        subprotocols=["sideword.v1"], headers={"Origin": origin},
    ) as socket:
        socket.send_json({"type": "auth", "token": users[0]["token"]})
        assert socket.receive_json() == {"type": "auth_error", "reason": "invalid session"}
