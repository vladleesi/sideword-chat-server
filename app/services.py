"""Shared business logic for HTTP and WebSocket routers."""

from __future__ import annotations

import base64
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .config import get_settings
from .invite_security import authenticate_password, resume_digest
from .message_keys import valid_public_key
from .models import (
    Chat,
    ChatMember,
    ChatType,
    Link,
    LinkType,
    User,
)
from .schemas import ChatInfo, ParticipantInfo
from .security import (
    generate_public_id,
    public_key_fingerprint,
)

_settings = get_settings()


def user_to_participant(user: User) -> ParticipantInfo:
    return ParticipantInfo(
        public_id=user.public_id,
        public_key=base64.b64encode(user.public_key).decode("ascii"),
        display_name=user.display_name,
        key_fingerprint=public_key_fingerprint(user.public_key),
    )


async def load_chat_info(session: AsyncSession, chat: Chat) -> ChatInfo:
    result = await session.execute(
        select(ChatMember)
        .where(ChatMember.chat_id == chat.id)
        .options(selectinload(ChatMember.user))
    )
    members = [m.user for m in result.scalars().all() if m.user is not None]
    if any(not valid_public_key(u.public_key) for u in members):
        raise HTTPException(409, "This room uses retired encryption. Create a new invite.")
    return ChatInfo(
        id=chat.id,
        chat_type=chat.chat_type.value,
        title=chat.title,
        created_at=chat.created_at,
        participants=[user_to_participant(u) for u in members],
    )


async def _create_user(
    session: AsyncSession, public_key: bytes, display_name: str | None
) -> User:
    user = User(
        public_id=generate_public_id(),
        display_name=(display_name or None),
        public_key=public_key,
    )
    session.add(user)
    await session.flush()
    return user


def _link_expired(link: Link) -> bool:
    if link.expires_at is None:
        return False
    exp = link.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp <= datetime.now(timezone.utc)


async def activate_link(
    session: AsyncSession,
    link_token: str,
    public_key: bytes,
    display_name: str | None,
    current_user: User | None = None,
    password: str | None = None,
    resume_credential: str | None = None,
) -> tuple[User, Chat, Link]:
    """Authenticate before allocating a slot, under BEGIN IMMEDIATE on SQLite."""

    result = await session.execute(
        select(Link).where(Link.token == link_token).with_for_update()
    )
    link = result.scalar_one_or_none()
    if link is None or link.is_deleted:
        raise HTTPException(status_code=404, detail="link not found")
    if link.revoked_at is not None:
        raise HTTPException(status_code=410, detail="link revoked")
    if _link_expired(link):
        link.is_active = False
        await session.commit()
        raise HTTPException(status_code=410, detail="link expired")
    chat = None
    if link.chat_id is not None:
        chat = await session.get(Chat, link.chat_id)

    credential_hash = resume_digest(resume_credential)
    members = []
    if chat is not None:
        members = list((await session.scalars(
            select(ChatMember).where(ChatMember.chat_id == chat.id)
            .options(selectinload(ChatMember.user))
        )).all())
        if any(m.user is not None and not valid_public_key(m.user.public_key) for m in members):
            raise HTTPException(409, "This room uses retired encryption. Create a new invite.")
        for member in members:
            authenticated = (
                current_user is not None and member.user_id == current_user.id
            ) or (credential_hash is not None and member.resume_hash == credential_hash)
            if authenticated:
                if member.user is None or not member.user.is_active:
                    raise HTTPException(401, "invalid or revoked participant")
                if member.user.public_key != public_key:
                    raise HTTPException(409, "public_key mismatch with existing participant")
                if member.resume_hash is None and credential_hash is not None:
                    # Upgrade a legacy participant while its JWT authenticates it.
                    member.resume_hash = credential_hash
                # Sealing only blocks new admissions, never an authenticated reconnect.
                await session.commit()
                return member.user, chat, link

    capacity = 2 if link.link_type is LinkType.personal else link.max_uses
    if not link.is_active or (capacity and max(link.uses_count, len(members)) >= capacity):
        raise HTTPException(410, "room sealed; reconnect with your saved session")

    if len(members) >= 101:
        raise HTTPException(429, "room participant capacity reached")
    if current_user is None and (await session.scalar(select(func.count(User.id)))) >= (
        get_settings().max_users
    ):
        raise HTTPException(429, "participant capacity reached")
    await authenticate_password(session, link, password)
    # A public key alone is not proof of identity. Never grant a reconnect based
    # on public data, nor let a lost response consume the same device's next slot.
    if any(m.user is not None and m.user.public_key == public_key for m in members):
        raise HTTPException(
            409, "This device has joined. Use its saved session or resume credential.",
        )

    if chat is None:
        chat = Chat(chat_type=ChatType(link.link_type.value))
        session.add(chat)
        await session.flush()
        link.chat_id = chat.id
    user = current_user or await _create_user(session, public_key, display_name)
    session.add(ChatMember(chat_id=chat.id, user_id=user.id, resume_hash=credential_hash))
    link.uses_count += 1
    if capacity and max(link.uses_count, len(members) + 1) >= capacity:
        link.is_active = False

    await session.commit()
    await session.refresh(user)
    await session.refresh(chat)
    return user, chat, link


async def get_user_chats(session: AsyncSession, user: User) -> list[Chat]:
    result = await session.execute(
        select(Chat)
        .join(ChatMember, ChatMember.chat_id == Chat.id)
        .where(ChatMember.user_id == user.id)
        .order_by(Chat.created_at.desc())
    )
    return list(result.scalars().all())


async def ensure_chat_member(
    session: AsyncSession, chat_id: int, user: User
) -> Chat:
    result = await session.execute(
        select(Chat)
        .join(ChatMember, ChatMember.chat_id == Chat.id)
        .where(Chat.id == chat_id, ChatMember.user_id == user.id)
    )
    chat = result.scalars().first()
    if chat is None:
        raise HTTPException(status_code=404, detail="chat not found")
    return chat


async def chat_members(session: AsyncSession, chat_id: int) -> list[User]:
    result = await session.execute(
        select(User)
        .join(ChatMember, ChatMember.user_id == User.id)
        .where(ChatMember.chat_id == chat_id)
    )
    members = list(result.scalars().all())
    if any(not valid_public_key(u.public_key) for u in members):
        raise HTTPException(409, "This room uses retired encryption. Create a new invite.")
    return members
