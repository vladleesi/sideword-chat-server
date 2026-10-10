"""Security boundary regressions; no running service or real credentials."""

import asyncio
import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient as RawClient
from sqlalchemy import delete, func, select
from test_exact_delivery import headers, poll, read, reference
from test_exact_delivery import room as _room

from app.config import get_settings
from app.db import SessionLocal
from app.main import create_app
from app.models import ClientSession, Link, LoginLimit, PendingMessage, RefreshUse, SendRecord
from app.security import decode_client_token

room = _room

def upload(client, users, content="eA=="):
    return client.post(f"/api/v1/chats/{users[0]['chat']['id']}/messages",
                       headers=headers(users[0]),
                       json={"client_message_id": "retry", "envelopes": [{
                           "recipient_public_id": u["user"]["public_id"], "ciphertext": content,
                       } for u in users[1:]]})


def test_send_retries_are_atomic_and_survive_read_deletion(room):
    client, users = room()
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda _: upload(client, users), range(2)))
    assert [r.status_code for r in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    messages = poll(client, users[1])["messages"]
    assert len(messages) == 1
    assert read(client, users[1], reference(messages[0])).json() == {"marked": 1}
    assert upload(client, users).json() == responses[0].json()
    assert poll(client, users[1])["messages"] == []
    assert upload(client, users, "eQ==").status_code == 409


def test_queue_quota_rejects_whole_fanout_without_ledger(room, monkeypatch):
    client, users = room(3)
    monkeypatch.setattr(get_settings(), "max_pending_messages", 1)
    assert upload(client, users).status_code == 429
    assert poll(client, users[1])["messages"] == []
    assert poll(client, users[2])["messages"] == []
    async def count():
        async with SessionLocal() as session:
            return await session.scalar(select(func.count(SendRecord.id)).where(
                SendRecord.chat_id == users[0]["chat"]["id"]))
    assert asyncio.run(count()) == 0


def test_partial_group_read_and_backup_keep_send_retry_evidence(room, tmp_path):
    from pathlib import Path
    client, users = room(3)
    first = upload(client, users)
    read(client, users[1], reference(poll(client, users[1])["messages"][0]))
    assert upload(client, users).json() == first.json()
    assert poll(client, users[1])["messages"] == []
    assert len(poll(client, users[2])["messages"]) == 1
    source = Path(get_settings().db_path).resolve()
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as original:
        with sqlite3.connect(tmp_path / "backup.sqlite3") as backup:
            original.backup(backup)
            row = backup.execute("SELECT payload_hash, recipients_json FROM send_records "
                                 "WHERE chat_id = ?", (users[0]["chat"]["id"],)).fetchone()
            assert row is not None and len(row[0]) == 64
            assert users[1]["user"]["public_id"] in row[1]


def test_retry_window_is_bounded_and_legacy_rows_fail_closed(room):
    client, users = room()
    first = upload(client, users)
    assert first.status_code == 200
    old = poll(client, users[1])["messages"][0]
    read(client, users[1], reference(old))
    async def expire():
        async with SessionLocal() as session:
            record = await session.scalar(select(SendRecord).where(
                SendRecord.chat_id == users[0]["chat"]["id"]))
            record.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await session.commit()
    asyncio.run(expire())
    assert upload(client, users, "eQ==").status_code == 200
    new = poll(client, users[1])["messages"][0]
    assert new["id"] == old["id"]
    assert read(client, users[1], reference(old)).json() == {"marked": 0}
    async def remove_ledger():
        async with SessionLocal() as session:
            await session.execute(delete(SendRecord).where(
                SendRecord.chat_id == users[0]["chat"]["id"]))
            await session.commit()
    asyncio.run(remove_ledger())
    assert upload(client, users).status_code == 409


def test_byte_quota_does_not_remove_previously_queued_rows(room, monkeypatch):
    client, users = room()
    assert upload(client, users).status_code == 200
    before = poll(client, users[1])["messages"]
    monkeypatch.setattr(get_settings(), "max_pending_bytes", 0)
    assert upload(client, users).status_code == 200  # Retry needs no new capacity.
    assert poll(client, users[1])["messages"] == before
    async def unchanged():
        async with SessionLocal() as session:
            return await session.scalar(select(func.count(PendingMessage.id)).where(
                PendingMessage.chat_id == users[0]["chat"]["id"]))
    assert asyncio.run(unchanged()) == 1


def test_removed_deletion_routes_cannot_consume_queued_messages(room):
    client, users = room()
    assert upload(client, users).status_code == 200
    message = poll(client, users[1])["messages"][0]
    assert client.post("/api/v1/ack", headers=headers(users[1]), json={
        "message_ids": [message["id"]],
    }).status_code == 404
    assert client.post(f"/api/v1/chats/{message['chat_id']}/read",
                       headers=headers(users[1]), json={
                           "client_message_ids": [message["client_message_id"]],
                       }).status_code == 404
    assert poll(client, users[1])["messages"] == [message]
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/ack" not in paths
    assert "/api/v1/chats/{chat_id}/read" not in paths
    assert "post" not in paths["/api/v1/sessions"]
    assert client.post("/api/v1/sessions", headers=headers(users[0]), json={
        "credential": secrets.token_urlsafe(32),
    }).status_code == 405
    assert client.post("/api/v1/ack/exact", headers=headers(users[1]), json={
        "messages": [reference(message)],
    }).json()["deleted_messages"] == 1


@pytest.mark.parametrize("session_claim", ["missing", None, "", 123, [], {}, "unknown", "foreign"])
def test_unregistered_tokens_rejected_across_http_ws_and_activation(room, session_claim):
    client, users = room()
    claims = decode_client_token(users[0]["token"])
    if session_claim == "missing":
        claims.pop("sid")
    else:
        claims["sid"] = (decode_client_token(users[1]["token"])["sid"]
                         if session_claim == "foreign" else session_claim)
    token = jwt.encode(claims, get_settings().secret_key, algorithm="HS256")
    denied = {"token": token}
    assert client.get("/api/v1/me", headers=headers(denied)).status_code == 401
    assert client.get("/api/v1/poll", headers=headers(denied)).status_code == 401
    async def invite():
        async with SessionLocal() as session:
            link = await session.get(Link, claims["lid"])
            return link.token, link.uses_count
    invite_token, before = asyncio.run(invite())
    assert client.post("/api/v1/links/activate", headers=headers(denied), json={
        "token": invite_token, "public_key": users[0]["user"]["public_key"],
        "session_credential": secrets.token_urlsafe(32),
    }).status_code == 401
    assert asyncio.run(invite())[1] == before
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": token})
        assert ws.receive_json()["type"] == "auth_error"


def csrf(client):
    client.get("/admin/login", follow_redirects=False)
    return {"X-CSRF-Token": client.cookies.get("sideword_csrf")}


def test_non_ascii_csrf_tokens_are_rejected_without_server_errors():
    from app.guards import valid_csrf

    assert not valid_csrf("\u00e9" * 113, "")
    with RawClient(create_app(), base_url="https://testserver") as client:
        csrf(client)
        response = client.post("/admin/login", data={
            "username": "admin", "password": "adminpass", "csrf_token": "\u00e9" * 113,
        }, headers={"Origin": "https://testserver"})
        assert response.status_code == 403


def test_admin_csrf_origin_cookie_flags_and_logout_revocation():
    with RawClient(create_app(), base_url="https://testserver") as client:
        credentials = {"username": "admin", "password": "adminpass"}
        assert client.post("/admin/login", data=credentials).status_code == 403
        response = client.post("/admin/login", data=credentials, headers=csrf(client),
                               follow_redirects=False)
        assert response.status_code == 303
        cookie_header = response.headers["set-cookie"]
        assert all(flag in cookie_header for flag in ("HttpOnly", "Secure", "SameSite=strict"))
        stolen = client.cookies.get("sideword_admin_session")
        token = csrf(client)
        assert client.post("/admin/logout", headers={**token, "Origin": "https://evil.test"}
                           ).status_code == 403
        assert client.post("/admin/logout", headers=token,
                           follow_redirects=False).status_code == 303
        client.cookies.clear()
        assert client.get("/admin/api/export", headers={"Authorization": f"Bearer {stolen}"}
                          ).status_code == 401


def test_login_throttle_persists_across_app_instances(monkeypatch):
    async def clear():
        async with SessionLocal() as session:
            await session.execute(delete(LoginLimit))
            await session.commit()
    asyncio.run(clear())
    monkeypatch.setattr(get_settings(), "login_attempts_per_minute", 2)
    for expected in (401, 401, 429):
        with RawClient(create_app(), base_url="https://testserver") as client:
            response = client.post("/admin/login", headers=csrf(client),
                                   data={"username": "unknown", "password": "incorrect"})
            assert response.status_code == expected


@pytest.mark.parametrize("origin", ["null", None])
def test_no_referrer_login_forms_require_same_origin_metadata_and_csrf(origin):
    import re
    with RawClient(create_app(), base_url="https://testserver") as client:
        page = client.get("/admin/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        data = {"username": "admin", "password": "adminpass", "csrf_token": token}
        origin_header = {"Origin": origin} if origin else {}
        for site in ("cross-site", "same-site", "none"):
            assert client.post("/admin/login", data=data, headers={
                **origin_header, "Sec-Fetch-Site": site,
            }, follow_redirects=False).status_code == 403
        trusted = {**origin_header, "Sec-Fetch-Site": "same-origin"}
        assert client.post("/admin/login", data={**data, "csrf_token": "invalid"},
                           headers=trusted, follow_redirects=False).status_code == 403
        assert client.post("/admin/login", data=data, headers={
            **trusted, "Origin": "https://evil.test",
        }, follow_redirects=False).status_code == 403
        assert client.post("/admin/login", data=data, headers=trusted,
                           follow_redirects=False).status_code == 303


def activate_session(client, user, credential):
    claims = decode_client_token(user["token"])
    async def invite():
        async with SessionLocal() as session:
            return (await session.get(Link, claims["lid"])).token
    return client.post("/api/v1/links/activate", headers=headers(user), json={
        "token": asyncio.run(invite()), "public_key": user["user"]["public_key"],
        "session_credential": credential,
    })


def rotate(client, old, new):
    return client.post("/api/v1/sessions/refresh", json={"credential": old, "next_credential": new})


def test_refresh_lost_responses_retry_and_replay_revokes_http_and_ws(room):
    client, users = room()
    old, new = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    first = activate_session(client, users[0], old)
    assert first.status_code == 200
    retried = activate_session(client, users[0], old)
    assert retried.json()["session_id"] == first.json()["session_id"]
    rotated = rotate(client, old, new)
    assert rotated.status_code == 200
    assert rotate(client, old, new).status_code == 200
    active = {"token": rotated.json()["token"]}
    assert client.get("/api/v1/me", headers=headers(active)).status_code == 200
    assert rotate(client, old, secrets.token_urlsafe(32)).status_code == 401
    assert client.get("/api/v1/me", headers=headers(active)).status_code == 401
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": active["token"]})
        assert ws.receive_json()["type"] == "auth_error"


def test_device_revocation_preserves_other_registered_sessions(room):
    client, users = room()
    old = secrets.token_urlsafe(32)
    issued = activate_session(client, users[0], old).json()
    url = "/api/v1/sessions/" + issued["session_id"]
    assert client.delete(url, headers=headers(users[1])).status_code == 404
    assert client.delete(url, headers=headers({"token": issued["token"]})).status_code == 200
    assert rotate(client, old, secrets.token_urlsafe(32)).status_code == 401
    assert client.get("/api/v1/me", headers=headers(issued)).status_code == 401
    assert client.get("/api/v1/me", headers=headers(users[0])).status_code == 200


def test_refresh_after_grace_revokes_and_hashes_only_are_stored(room):
    client, users = room()
    old, new = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    issued = activate_session(client, users[0], old).json()
    assert rotate(client, old, new).status_code == 200
    async def expire():
        async with SessionLocal() as session:
            record = await session.get(ClientSession, issued["session_id"])
            assert record.refresh_hash not in (old, new)
            use = await session.scalar(select(RefreshUse).where(RefreshUse.session_id == record.id))
            assert use.token_hash != old and use.next_hash != new
            use.retry_until = datetime.now(timezone.utc) - timedelta(seconds=1)
            await session.commit()
    asyncio.run(expire())
    assert rotate(client, old, new).status_code == 401


def test_streamed_body_rate_and_tls_limits(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "max_request_bytes", 1024)
    with RawClient(create_app()) as client:
        assert client.post("/api/v1/ack", content=iter([b"a" * 700, b"b" * 700])
                           ).status_code == 413
    monkeypatch.setattr(settings, "requests_per_minute", 2)
    with RawClient(create_app()) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/health").status_code == 200
        assert client.get("/health").status_code == 429
    monkeypatch.setattr(settings, "require_https", True)
    with RawClient(create_app()) as client:
        assert client.get("/health", headers={"Host": "localhost"}).status_code == 403
    with RawClient(create_app(), base_url="https://testserver") as client:
        assert client.get("/health").status_code == 200


def test_private_admin_rejects_remote_http_and_websocket(monkeypatch):
    from starlette.websockets import WebSocketDisconnect
    monkeypatch.setattr(get_settings(), "private_admin", True)
    with RawClient(create_app(), base_url="https://testserver") as client:
        for path in ("/admin/login", "/admin/api/export", "/docs", "/openapi.json"):
            assert client.get(path).status_code == 404
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/admin/login"):
                pass


def test_ws_connection_and_frame_limits(room, monkeypatch):
    from starlette.websockets import WebSocketDisconnect
    client, users = room()
    monkeypatch.setattr(get_settings(), "max_ws_per_user", 1)
    with client.websocket_connect("/ws") as first:
        first.send_json({"type": "auth", "token": users[0]["token"]})
        assert first.receive_json()["type"] == "hello"
        with client.websocket_connect("/ws") as second:
            second.send_json({"type": "auth", "token": users[0]["token"]})
            with pytest.raises(WebSocketDisconnect):
                second.receive_json()
        first.send_text("x" * 4097)
        with pytest.raises(WebSocketDisconnect):
            first.receive_json()
