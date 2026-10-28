"""Roster loading avoids extra user queries while retaining admission/key checks."""

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from message_key_fixtures import public_key
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import Chat, ChatMember, ChatType, Link, LinkType, User
from app.services import activate_link, load_chat_info


@pytest.fixture
def roster_store(tmp_path):
    # This engine never uses the configured/running instance's database.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rosters.sqlite3'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def create():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            chat = Chat(chat_type=ChatType.group, title="Roster fixture")
            users = [User(public_id=f"fixture-{i}", public_key=public_key(),
                          display_name="Same name", is_active=i != 1) for i in range(3)]
            session.add_all([chat, *users])
            await session.flush()
            session.add_all(ChatMember(chat_id=chat.id, user_id=user.id,
                                       display_name="Room name") for user in users)
            await session.commit()
            return chat.id, users[0].id

    chat_id, user_id = asyncio.run(create())
    statements = []

    def count_queries(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", count_queries)
    yield sessions, chat_id, user_id, statements
    asyncio.run(engine.dispose())


@pytest.mark.parametrize("state", ["open", "closed", "empty", "missing_user", "invalid_key"])
def test_roster_uses_one_select_and_preserves_members_and_key_validation(roster_store, state):
    sessions, chat_id, user_id, statements = roster_store

    async def check():
        async with sessions() as session:
            chat = await session.get(Chat, chat_id)
            if state == "closed":
                chat.closed_at = datetime(2026, 10, 15, tzinfo=timezone.utc)
            elif state == "empty":
                chat = Chat(chat_type=ChatType.personal)
                session.add(chat)
            elif state == "missing_user":
                # Historical orphan rows must still be skipped, using an outer join.
                session.add(ChatMember(chat_id=chat.id, user_id=999999))
            elif state == "invalid_key":
                (await session.get(User, user_id)).public_key = b"invalid point"
            await session.commit()
            statements.clear()
            if state == "invalid_key":
                with pytest.raises(HTTPException) as error:
                    await load_chat_info(session, chat)
                assert error.value.status_code == 409
            else:
                info = await load_chat_info(session, chat)
                assert info.id == chat.id
                assert info.title == chat.title
                assert info.chat_type == chat.chat_type.value
                assert info.created_at == chat.created_at
                assert info.closed_at == chat.closed_at
                assert [peer.public_id for peer in info.participants] == (
                    [] if state == "empty" else [f"fixture-{i}" for i in range(3)]
                )
                assert all(peer.display_name == "Room name" for peer in info.participants)
                assert all(peer.public_key and peer.key_fingerprint for peer in info.participants)
            assert len(statements) == 1

    asyncio.run(check())


def test_consumed_invite_reconnect_uses_joined_roster_and_preserves_key_matching(roster_store):
    sessions, chat_id, user_id, statements = roster_store

    async def check():
        async with sessions() as session:
            user = await session.get(User, user_id)
            link = Link(token="synthetic-consumed-invite", link_type=LinkType.group,
                        chat_id=chat_id, max_uses=3, uses_count=3, is_active=False)
            session.add(link)
            await session.commit()
            statements.clear()
            restored, chat, current_link = await activate_link(
                session, link.token, user.public_key, None, current_user=user,
            )
            assert restored.id == user_id and chat.id == chat_id
            assert current_link.uses_count == 3 and not current_link.is_active
            roster_queries = [statement for statement in statements
                              if "FROM chat_members" in statement]
            assert len(roster_queries) == 1
            assert "LEFT OUTER JOIN users" in roster_queries[0]
            with pytest.raises(HTTPException) as error:
                await activate_link(session, link.token, public_key(), None, current_user=user)
            assert error.value.status_code == 409
            assert current_link.uses_count == 3

    asyncio.run(check())
