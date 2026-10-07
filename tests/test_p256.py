"""Breaking test-protocol replacement preserves authentication and admission boundaries."""

import asyncio
import base64
import json
import secrets

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from message_key_fixtures import public_key
from sqlalchemy import select
from test_exact_delivery import headers
from test_exact_delivery import room as _room
from test_security_stages import bootstrap

from app.db import SessionLocal
from app.message_keys import valid_public_key
from app.models import Link, User
from app.routers.ws import _resolve_user
from app.security import decode_client_token

room = _room


def encoded(value):
    return base64.b64encode(value).decode()


@pytest.mark.parametrize("value", [
    b"", bytes(32), b"\x04" + bytes(64), b"\x02" + bytes(64), b"\x02" + bytes(32),
    b"\x04" + bytes([255]) * 64,
    ec.generate_private_key(ec.SECP384R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint),
])
def test_invalid_points_rejected_before_allocating_an_invite_slot(room, value):
    client, users = room(1)
    async def create():
        async with SessionLocal() as session:
            link = await session.get(Link, decode_client_token(users[0]["token"])["lid"])
            return link.token, link.uses_count
    token, count = asyncio.run(create())
    assert not valid_public_key(value)
    response = client.post("/api/v1/links/activate", json={
        "token": token, "public_key": encoded(value),
    })
    assert response.status_code == 422
    assert asyncio.run(create())[1] == count


def test_retired_device_cannot_authenticate_renew_or_join_and_new_device_cannot_mix_curves(room):
    client, users = room(2)
    credential = secrets.token_urlsafe(32)
    response = bootstrap(client, users[0], credential)
    assert response.status_code == 200
    renewable = response.json()
    async def state(key=None):
        async with SessionLocal() as session:
            user = await session.scalar(select(User).where(
                User.public_id == users[0]["user"]["public_id"]))
            link = await session.get(Link, decode_client_token(users[0]["token"])["lid"])
            if key is not None:
                user.public_key = key
                link.max_uses = 3
                link.is_active = True
                await session.commit()
            return link.token, link.uses_count
    token, count = asyncio.run(state(bytes(32)))
    try:
        assert client.get("/api/v1/me", headers=headers(users[0])).status_code == 401
        assert client.get("/api/v1/me", headers=headers(renewable)).status_code == 401
        assert bootstrap(client, users[0], secrets.token_urlsafe(32)).status_code == 401
        assert client.post("/api/v1/sessions/refresh", json={
            "credential": credential, "next_credential": secrets.token_urlsafe(32),
        }).status_code == 401
        async def resolve_socket():
            async with SessionLocal() as session:
                return await _resolve_user(session, users[0]["token"])
        assert asyncio.run(resolve_socket()) is None
        response = client.post("/api/v1/links/activate", json={
            "token": token, "public_key": encoded(public_key()),
        })
        assert response.status_code == 409
        assert "new invite" in response.json()["detail"]
        assert asyncio.run(state())[1] == count
        assert client.get("/api/v1/me", headers=headers(users[1])).status_code == 409
        assert client.post(f"/api/v1/chats/{users[1]['chat']['id']}/messages", json={
            "client_message_id": "retired-room",
            "envelopes": [{"recipient_public_id": users[0]["user"]["public_id"],
                           "ciphertext": "eA=="}],
        }, headers=headers(users[1])).status_code == 409
    finally:
        asyncio.run(state(base64.b64decode(users[0]["user"]["public_key"])))


@pytest.mark.parametrize("value", [bytes(32), b"\x04" + bytes(64), b"\x02" + bytes(32)])
def test_configuration_import_rejects_invalid_keys_before_replace_deletes_data(room, value):
    client, _ = room(2)
    assert client.post("/admin/login", data={"username": "admin", "password": "adminpass"},
                       follow_redirects=False).status_code == 303
    original = client.get("/admin/api/export").json()
    original["users"][0]["public_key"] = encoded(value)
    before = client.get("/admin/api/export").json()
    response = client.post("/admin/api/import?replace=true", files={
        "bundle": ("test.json", json.dumps(original), "application/json"),
    })
    assert response.status_code == 400
    after = client.get("/admin/api/export").json()
    for field in ("users", "chats", "links"):
        assert after[field] == before[field]
