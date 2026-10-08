"""Async SQLAlchemy engine and session factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from .config import get_settings


class Base(DeclarativeBase):
    """Declarative base for ORM models."""


_settings = get_settings()

engine = create_async_engine(
    _settings.db_url,
    echo=False,
    hide_parameters=True,
    future=True,
    pool_pre_ping=True,
    connect_args={"timeout": 30},
)

SessionLocal = async_sessionmaker(
    engine,
    expire_on_commit=False,
    autoflush=False,
    class_=AsyncSession,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields a database session."""

    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Async context manager for sessions outside FastAPI."""

    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    """Create tables if they do not exist."""

    from . import models  # noqa: F401 — register models

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Delivery identities survive row-ID reuse and remain stable across
        # restarts/backups. Existing queued rows are backfilled exactly once.
        for table in ("pending_messages", "read_receipts"):
            columns = await conn.execute(text(f"PRAGMA table_info({table})"))
            if "delivery_id" not in {row[1] for row in columns}:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN delivery_id VARCHAR(32)"))
            await conn.execute(text(
                f"UPDATE {table} SET delivery_id = lower(hex(randomblob(16))) "
                "WHERE delivery_id IS NULL"
            ))
            await conn.execute(text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS ix_{table}_delivery_id ON {table}(delivery_id)"
            ))
        columns = await conn.execute(text("PRAGMA table_info(read_receipts)"))
        if "message_delivery_id" not in {row[1] for row in columns}:
            await conn.execute(text(
                "ALTER TABLE read_receipts ADD COLUMN message_delivery_id VARCHAR(32)"
            ))
        columns = await conn.execute(text("PRAGMA table_info(send_records)"))
        if "receipt_state_json" not in {row[1] for row in columns}:
            await conn.execute(text(
                "ALTER TABLE send_records ADD COLUMN receipt_state_json TEXT NOT NULL DEFAULT '{}'"
            ))
        # Lightweight in-place migration for installations created before
        # invite revocation was tied to client sessions. Existing inactive
        # links are treated as revoked once, which safely invalidates legacy
        # JWTs that did not carry an invite id.
        columns = await conn.execute(text("PRAGMA table_info(links)"))
        column_names = {row[1] for row in columns}
        for name, sql_type in (
            ("password_hash", "TEXT"),
            ("failed_attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("failed_window_started_at", "DATETIME"),
        ):
            if name not in column_names:
                await conn.execute(text(f"ALTER TABLE links ADD COLUMN {name} {sql_type}"))
        member_columns = await conn.execute(text("PRAGMA table_info(chat_members)"))
        if "resume_hash" not in {row[1] for row in member_columns}:
            await conn.execute(text("ALTER TABLE chat_members ADD COLUMN resume_hash VARCHAR(64)"))
        if "is_deleted" not in column_names:
            await conn.execute(text(
                "ALTER TABLE links ADD COLUMN is_deleted BOOLEAN NOT NULL DEFAULT 0"
            ))
        if "revoked_at" not in column_names:
            await conn.execute(text("ALTER TABLE links ADD COLUMN revoked_at DATETIME"))
            await conn.execute(
                text(
                    "UPDATE links SET revoked_at = CURRENT_TIMESTAMP "
                    "WHERE is_active = 0"
                )
            )
