"""Async SQLAlchemy engine and session factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, inspect
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
    pool_pre_ping=True,
    connect_args={"timeout": 30},
)


@event.listens_for(engine.sync_engine, "connect")
def configure_storage(connection, _record):
    """Apply deletion policy to every pooled connection without disabling crash recovery."""
    cursor = connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode")
        if cursor.fetchone()[0].lower() == "wal":
            raise RuntimeError(
                "WAL databases are unsupported by this deletion policy. Use fresh state."
            )
        for statement, expected in (
            ("PRAGMA secure_delete=ON", 1),
            ("PRAGMA journal_mode=TRUNCATE", "truncate"),
            ("PRAGMA journal_size_limit=0", 0),
        ):
            cursor.execute(statement)
            if cursor.fetchone()[0] != expected:
                raise RuntimeError("SQLite cannot enforce the required deletion policy.")
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.execute("PRAGMA temp_store=MEMORY")
    finally:
        cursor.close()


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

    def create_schema(connection):
        inspector = inspect(connection)
        for table in Base.metadata.sorted_tables:
            if inspector.has_table(table.name) and {
                column["name"] for column in inspector.get_columns(table.name)
            } != set(table.columns.keys()):
                raise RuntimeError(
                    "Database schema does not match this pre-release. Use a fresh "
                    "database and preserve the existing database separately."
                )
        Base.metadata.create_all(connection)

    async with engine.begin() as conn:
        await conn.run_sync(create_schema)
