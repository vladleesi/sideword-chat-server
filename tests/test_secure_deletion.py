"""Deletion/backup evidence uses synthetic markers and isolated databases only."""

import asyncio
import base64
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text
from test_exact_delivery import headers, poll, reference, send
from test_exact_delivery import room as _room

from app.cleanup import _purge_once
from app.config import get_settings
from app.db import SessionLocal, engine
from app.models import PendingMessage, SendRecord, User
from app.routers.ws import _backlog_payload
from scripts.deploy import backup_database

room = _room


def upload_marker(client, users, marker):
    return client.post(f"/api/v1/chats/{users[0]['chat']['id']}/messages",
                       headers=headers(users[0]), json={
                           "client_message_id": "deletion-fixture",
                           "envelopes": [{"recipient_public_id": u["user"]["public_id"],
                                          "ciphertext": base64.b64encode(marker).decode()}
                                         for u in users[1:]],
                       })


def test_every_connection_enforces_deletion_and_crash_safe_journals(room):
    room()
    async def settings():
        async with engine.connect() as first, engine.connect() as second:
            for conn in (first, second):
                for name, expected in [("secure_delete", 1), ("journal_mode", "truncate"),
                                       ("journal_size_limit", 0), ("synchronous", 2),
                                       ("temp_store", 2)]:
                    assert (await conn.execute(text(f"PRAGMA {name}"))).scalar() == expected
    asyncio.run(settings())


def test_ack_scrubs_database_and_truncates_journal_without_losing_group_peer(room):
    client, users = room(3)
    marker = b'synthetic-ciphertext-deletion-evidence-' * 3000
    accepted = upload_marker(client, users, marker)
    assert accepted.status_code == 200
    database = Path(get_settings().db_path)
    assert marker[:128] in database.read_bytes()
    for index in [1, 2]:
        message = poll(client, users[index])["messages"][0]
        assert client.post("/api/v1/ack/exact", headers=headers(users[index]), json={
            "messages": [reference(message)],
        }).json()["deleted_messages"] == 1
        assert (marker[:128] in database.read_bytes()) is (index == 1)
        journal = Path(str(database) + '-journal')
        assert not journal.exists() or journal.stat().st_size == 0
        assert not Path(str(database) + '-wal').exists()
    # Retry evidence remains: ACK cannot cause an identical retry to recreate ciphertext.
    assert upload_marker(client, users, marker).json() == accepted.json()
    assert poll(client, users[1])["messages"] == []


def test_content_expiry_is_fixed_and_expired_content_is_not_delivered(room, monkeypatch):
    client, users = room()
    marker = b'synthetic-expiry-evidence-' * 3000
    accepted = upload_marker(client, users, marker).json()
    deadline = datetime.fromisoformat(accepted["content_expires_at"])
    created = datetime.fromisoformat(accepted["created_at"])
    assert deadline - created == timedelta(days=7)
    monkeypatch.setattr(get_settings(), "message_ttl_days", 30)
    retried = upload_marker(client, users, marker).json()
    assert retried["content_expires_at"] == accepted["content_expires_at"]

    async def expire():
        async with SessionLocal() as session:
            message = await session.scalar(select(PendingMessage).where(
                PendingMessage.chat_id == users[0]["chat"]["id"]))
            record = await session.scalar(select(SendRecord).where(
                SendRecord.chat_id == users[0]["chat"]["id"]))
            past = datetime.now(timezone.utc) - timedelta(seconds=1)
            message.expires_at = record.content_expires_at = past
            await session.commit()
            user = await session.scalar(select(User).where(
                User.public_id == users[1]["user"]["public_id"]))
            assert (await _backlog_payload(session, user))[0] == []
    asyncio.run(expire())
    assert poll(client, users[1])["messages"] == []
    status = client.post(f"/api/v1/chats/{users[0]['chat']['id']}/messages/status",
                         headers=headers(users[0]),
                         json={"client_message_ids": ["deletion-fixture"]})
    assert status.json()["statuses"][0]["recipients"][0]["state"] == "expired"
    asyncio.run(_purge_once())
    assert marker[:128] not in Path(get_settings().db_path).read_bytes()
    # Expiry never re-enqueues an accepted message while its retry record is retained.
    assert upload_marker(client, users, marker).status_code == 200
    assert poll(client, users[1])["messages"] == []


def test_restart_preserves_pending_content_and_backup_excludes_it(room, tmp_path):
    client, users = room()
    marker = b'synthetic-backup-exclusion-evidence-' * 3000
    assert upload_marker(client, users, marker).status_code == 200
    before = poll(client, users[1])["messages"]
    asyncio.run(engine.dispose())
    assert poll(client, users[1])["messages"] == before
    destination = tmp_path / 'metadata.sqlite3'
    backup_database(Path(get_settings().db_path), destination)
    assert marker[:128] not in destination.read_bytes()
    assert destination.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(destination) as backup:
        assert backup.execute('SELECT COUNT(*) FROM pending_messages').fetchone() == (0,)
        assert backup.execute('SELECT COUNT(*) FROM users').fetchone()[0] > 0
        assert backup.execute('SELECT COUNT(*) FROM send_records').fetchone()[0] > 0
        assert backup.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    assert poll(client, users[1])["messages"] == before


def test_logical_backup_does_not_copy_deleted_free_pages_or_overwrite_existing_files(tmp_path):
    source, target = tmp_path / 'source.sqlite3', tmp_path / 'backup.sqlite3'
    live = b'synthetic-live-queue-marker' * 3000
    deleted = b'synthetic-deleted-queue-marker' * 3000
    with sqlite3.connect(source) as conn:
        conn.execute('PRAGMA secure_delete=OFF')
        conn.execute('CREATE TABLE pending_messages (ciphertext BLOB)')
        conn.execute('CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT)')
        conn.execute('INSERT INTO accounts VALUES (?, ?)', (1, "synthetic 'quoted' user"))
        conn.executemany('INSERT INTO pending_messages VALUES (?)', [(live,), (deleted,)])
        conn.commit()
        conn.execute('DELETE FROM pending_messages WHERE ciphertext=?', (deleted,))
    assert deleted[:128] in source.read_bytes()
    backup_database(source, target)
    assert live[:128] not in target.read_bytes() and deleted[:128] not in target.read_bytes()
    with sqlite3.connect(target) as conn:
        assert conn.execute('SELECT * FROM accounts').fetchall() == [(1, "synthetic 'quoted' user")]
    before = target.read_bytes()
    with pytest.raises(FileExistsError):
        backup_database(source, target)
    assert target.read_bytes() == before


def test_missing_queue_copy_is_reported_unavailable_without_false_delivery(room):
    client, users = room()
    send(client, users)
    result = client.delete(f"/api/v1/chats/{users[0]['chat']['id']}/outbox",
                           headers=headers(users[0]))
    assert result.status_code == 200
    status = client.post(f"/api/v1/chats/{users[0]['chat']['id']}/messages/status",
                         headers=headers(users[0]), json={"client_message_ids": ["same-id"]})
    peer = status.json()["statuses"][0]["recipients"][0]
    assert peer["state"] == "unavailable" and peer["delivered_at"] is None


def test_wal_mode_is_rejected_without_converting_existing_storage(tmp_path):
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db import configure_storage

    database = tmp_path / 'wal.sqlite3'
    with sqlite3.connect(database) as conn:
        assert conn.execute('PRAGMA journal_mode=WAL').fetchone() == ('wal',)
        conn.execute('CREATE TABLE synthetic (value TEXT)')
        conn.execute("INSERT INTO synthetic VALUES ('keep')")

    async def reject():
        isolated = create_async_engine(f'sqlite+aiosqlite:///{database}')
        event.listen(isolated.sync_engine, 'connect', configure_storage)
        try:
            with pytest.raises(RuntimeError, match='WAL databases are unsupported'):
                async with isolated.connect():
                    pass
        finally:
            await isolated.dispose()
    asyncio.run(reject())
    with sqlite3.connect(database) as conn:
        assert conn.execute('PRAGMA journal_mode').fetchone() == ('wal',)
        assert conn.execute('SELECT value FROM synthetic').fetchone() == ('keep',)
