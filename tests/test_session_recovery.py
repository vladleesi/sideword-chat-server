"""Restore authentication state only into dedicated temporary SQLite databases."""

import asyncio
import base64
import secrets
import sqlite3

import pytest
from csrf_client import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_exact_delivery import headers, poll, send
from test_security_stages import bootstrap, rotate

from app import db
from app.config import get_settings
from app.main import create_app
from app.models import Link, LinkType
from app.routers import ws


@pytest.fixture
def recovery_database(tmp_path, monkeypatch):
    engines = []

    def connect(name):
        path = (tmp_path / name).resolve()
        assert path.is_relative_to(tmp_path.resolve())
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}", hide_parameters=True)
        engines.append(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        monkeypatch.setattr(db, "engine", engine)
        monkeypatch.setattr(db, "SessionLocal", factory)
        monkeypatch.setattr(ws, "SessionLocal", factory)
        monkeypatch.setattr(get_settings(), "db_path", str(path))
        return path, engine

    monkeypatch.setattr(get_settings(), "secret_key", secrets.token_urlsafe(32))
    monkeypatch.setattr(get_settings(), "exports_dir", str(tmp_path / "exports"))
    yield connect
    for engine in engines:
        asyncio.run(engine.dispose())


@pytest.mark.parametrize("rotate_signing_key,clear_sessions", [
    (False, False), (True, False), (False, True), (True, True),
])
def test_restore_requires_key_rotation_and_session_removal(
    recovery_database, tmp_path, monkeypatch, rotate_signing_key, clear_sessions,
):
    original, engine = recovery_database("original.sqlite3")
    backup = tmp_path / "restored.sqlite3"
    admissions = [{
        "public_key": base64.b64encode(secrets.token_bytes(32)).decode(),
        "resume_credential": secrets.token_urlsafe(32),
    } for _ in range(2)]
    old, current = secrets.token_urlsafe(32), secrets.token_urlsafe(32)

    with TestClient(create_app()) as client:
        async def invite():
            async with db.SessionLocal() as session:
                link = Link(token=secrets.token_urlsafe(32), link_type=LinkType.personal,
                            max_uses=2, uses_count=0, is_active=True)
                session.add(link)
                await session.commit()
                return link.token
        invite_token = asyncio.run(invite())
        users = []
        for admission in admissions:
            response = client.post("/api/v1/links/activate",
                                   json={**admission, "token": invite_token})
            assert response.status_code == 200
            users.append(response.json())
        issued = bootstrap(client, users[0], old).json()
        assert rotate(client, old, current).status_code == 200
        assert client.post("/admin/login", data={
            "username": "admin", "password": "adminpass",
        }, follow_redirects=False).status_code == 303
        admin_token = client.cookies.get("sideword_admin_session")
        send(client, users)
        delivery = poll(client, users[1])["messages"][0]

        # Online backup API takes a consistent snapshot of this fixture's DB,
        # never a copied live database file or an operator-configured path.
        with sqlite3.connect(original.as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(backup) as target:
                source.backup(target)

        # These later revocations are deliberately absent from the snapshot.
        assert client.delete("/api/v1/sessions/" + issued["session_id"],
                             headers=headers(users[0])).status_code == 200
        assert rotate(client, current, secrets.token_urlsafe(32)).status_code == 401
        assert client.post("/admin/logout", follow_redirects=False).status_code == 303
        client.cookies.clear()
        assert client.get("/admin/api/export", headers={
            "Authorization": f"Bearer {admin_token}",
        }).status_code == 401
    asyncio.run(engine.dispose())

    # Exercise each omitted recovery step as well as the complete procedure.
    # All destructive statements target only the fixture's restored copy.
    assert backup.resolve().is_relative_to(tmp_path.resolve())
    if clear_sessions:
        with sqlite3.connect(backup) as restored:
            restored.execute("PRAGMA foreign_keys = ON")
            restored.execute("BEGIN IMMEDIATE")
            restored.execute("DELETE FROM refresh_uses")
            restored.execute("DELETE FROM client_sessions")
            restored.execute("DELETE FROM admin_sessions")
    if rotate_signing_key:
        monkeypatch.setattr(get_settings(), "secret_key", secrets.token_urlsafe(32))
    recovery_database("restored.sqlite3")

    with TestClient(create_app()) as client:
        legacy_status = 401 if rotate_signing_key else 200
        registered_status = 401 if rotate_signing_key or clear_sessions else 200
        assert client.get("/api/v1/me", headers=headers(users[0])).status_code == legacy_status
        assert client.get("/api/v1/me", headers=headers(issued)).status_code == registered_status
        assert client.get("/admin/api/export", headers={
            "Authorization": f"Bearer {admin_token}",
        }).status_code == registered_status
        for token, expected in ((users[0]["token"], legacy_status),
                                (issued["token"], registered_status)):
            with client.websocket_connect("/ws") as socket:
                socket.send_json({"type": "auth", "token": token})
                assert socket.receive_json()["type"] == (
                    "hello" if expected == 200 else "auth_error"
                )

        # Even a new signing key cannot invalidate a restored refresh secret.
        retried = rotate(client, old, current)
        assert retried.status_code == (401 if clear_sessions else 200)
        refreshed = rotate(client, current, secrets.token_urlsafe(32))
        assert refreshed.status_code == (401 if clear_sessions else 200)
        if not clear_sessions:
            assert client.get("/api/v1/me", headers=headers(refreshed.json())).status_code == 200

        if rotate_signing_key and clear_sessions:
            recovered = []
            for index, admission in enumerate(admissions):
                response = client.post("/api/v1/links/activate", json={
                    **admission, "token": invite_token,
                    "session_credential": secrets.token_urlsafe(32),
                })
                assert response.status_code == 200
                recovered.append(response.json())
                assert recovered[-1]["user"]["public_id"] == users[index]["user"]["public_id"]
                assert recovered[-1]["chat"]["id"] == users[index]["chat"]["id"]
            assert (
                poll(client, recovered[1])["messages"][0]["delivery_id"] == delivery["delivery_id"]
            )
            # The original retry evidence survives authentication cleanup.
            send(client, recovered)
            assert len(poll(client, recovered[1])["messages"]) == 1
            with sqlite3.connect(backup) as restored:
                assert restored.execute("SELECT count(*) FROM chat_members").fetchone()[0] == 2
                assert restored.execute("SELECT count(*) FROM send_records").fetchone()[0] == 1
            assert client.post("/admin/login", data={
                "username": "admin", "password": "adminpass",
            }, follow_redirects=False).status_code == 303
