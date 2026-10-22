"""Admin issuance/reset races against the isolated pytest database."""

import asyncio
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.db import SessionLocal
from app.main import create_app
from app.models import Admin, AdminSession
from app.routers import admin_auth
from scripts.create_admin import _run


@pytest.fixture
def account():
    username = "race-" + secrets.token_hex(8)
    with TestClient(create_app()) as client:
        asyncio.run(_run(username, "old-test-password"))
        yield client, username

        async def cleanup():
            async with SessionLocal() as session:
                admin_id = await session.scalar(select(Admin.id).where(Admin.username == username))
                await session.execute(delete(AdminSession).where(AdminSession.admin_id == admin_id))
                await session.execute(delete(Admin).where(Admin.id == admin_id))
                await session.commit()
        asyncio.run(cleanup())


def login(client, username, password="old-test-password"):
    client.get("/admin/login", follow_redirects=False)
    return client.post(
        "/admin/login", data={"username": username, "password": password},
        headers={"X-CSRF-Token": client.cookies.get("sideword_csrf")}, follow_redirects=False,
    )


def session_count(username=None):
    async def count():
        async with SessionLocal() as session:
            query = select(func.count(AdminSession.id))
            if username is not None:
                query = query.where(AdminSession.admin_id.in_(
                    select(Admin.id).where(Admin.username == username)
                ))
            return await session.scalar(query)
    return asyncio.run(count())


def test_password_reset_during_verification_cannot_issue_stale_session(account, monkeypatch):
    client, username = account
    verified, resume = Event(), Event()
    verify = admin_auth.verify_password

    def paused_verify(password, password_hash):
        result = verify(password, password_hash)
        verified.set()
        assert resume.wait(15), "password reset did not release verification"
        return result

    monkeypatch.setattr(admin_auth, "verify_password", paused_verify)
    with ThreadPoolExecutor(1) as pool:
        attempt = pool.submit(login, client, username)
        try:
            assert verified.wait(15), "login did not reach password verification"
            assert asyncio.run(_run(username, "new-test-password")) == 0
        finally:
            resume.set()
        response = attempt.result(timeout=15)
    assert response.status_code == 401
    assert "sideword_admin_session" not in client.cookies
    assert session_count(username) == 0
    assert login(client, username, "new-test-password").status_code == 303


def test_password_reset_after_issuance_revokes_cookie_and_bearer(account):
    client, username = account
    assert login(client, username).status_code == 303
    token = client.cookies.get("sideword_admin_session")
    assert asyncio.run(_run(username, "new-test-password")) == 0
    assert session_count(username) == 0
    assert client.get("/admin/", follow_redirects=False).status_code == 303
    client.cookies.clear()
    assert client.get("/admin/api/export", headers={
        "Authorization": f"Bearer {token}",
    }).status_code == 401
    assert login(client, username).status_code == 401
    assert login(client, username, "new-test-password").status_code == 303


def test_overlong_reset_preserves_existing_password_and_session(account):
    client, username = account
    assert login(client, username).status_code == 303
    before = session_count(username)
    with pytest.raises(ValueError, match="72 UTF-8 bytes"):
        asyncio.run(_run(username, "é" * 37))
    assert session_count(username) == before
    assert client.get("/admin/", follow_redirects=False).status_code == 200
    assert login(client, username).status_code == 303


def test_bootstrap_rejects_overlong_new_password_but_preserves_existing_admin(
    account, monkeypatch,
):
    from types import SimpleNamespace

    from app import admin_setup

    client, username = account
    settings = SimpleNamespace(admin_username=username, admin_password="é" * 37)
    monkeypatch.setattr(admin_setup, "get_settings", lambda: settings)
    asyncio.run(admin_setup.ensure_default_admin())
    assert login(client, username).status_code == 303
    settings.admin_username = "new-" + secrets.token_hex(8)
    with pytest.raises(ValueError, match="72 UTF-8 bytes"):
        asyncio.run(admin_setup.ensure_default_admin())

    async def absent():
        async with SessionLocal() as session:
            assert await session.scalar(select(Admin.id).where(
                Admin.username == settings.admin_username,
            )) is None

    asyncio.run(absent())


def fill_sessions(username, total, *, expired=False):
    async def fill():
        async with SessionLocal() as session:
            admin_id = await session.scalar(select(Admin.id).where(Admin.username == username))
            existing = await session.scalar(select(func.count(AdminSession.id)))
            expiry = datetime.now(timezone.utc) + timedelta(hours=-1 if expired else 1)
            session.add_all([
                AdminSession(id=secrets.token_hex(16), admin_id=admin_id, expires_at=expiry)
                for _ in range(total - existing)
            ])
            await session.commit()
    asyncio.run(fill())


def test_concurrent_logins_cannot_exceed_session_capacity(account, monkeypatch):
    _, username = account
    fill_sessions(username, 999)
    ready = Barrier(4)
    verify = admin_auth.verify_password

    def synchronized_verify(password, password_hash):
        result = verify(password, password_hash)
        ready.wait(timeout=15)
        return result

    monkeypatch.setattr(admin_auth, "verify_password", synchronized_verify)

    def attempt(_):
        # Separate app instances model workers sharing the same SQLite database.
        with TestClient(create_app()) as client:
            return login(client, username).status_code

    with ThreadPoolExecutor(4) as pool:
        statuses = list(pool.map(attempt, range(4)))
    assert sorted(statuses) == [303, 429, 429, 429]
    assert session_count() == 1000


def test_expired_sessions_do_not_block_new_login(account):
    client, username = account
    fill_sessions(username, 1000, expired=True)
    assert login(client, username).status_code == 303
    assert session_count(username) == 1
