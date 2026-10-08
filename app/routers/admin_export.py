"""Import/export server configuration (no message history)."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db import get_session
from ..deps import get_current_admin
from ..invite_security import valid_verifier
from ..message_keys import valid_public_key
from ..models import (
    Admin,
    Chat,
    ChatMember,
    ChatType,
    ClientSession,
    Link,
    LinkType,
    PendingMessage,
    ReadReceipt,
    RefreshUse,
    SendRecord,
    User,
)

router = APIRouter(prefix="/admin/api", tags=["admin-api"])


def _dt_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


@router.get("/export")
async def export_bundle(
    admin: Admin = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    users_result = await session.execute(select(User).order_by(User.id.asc()))
    users = list(users_result.scalars().all())

    chats_result = await session.execute(
        select(Chat)
        .options(selectinload(Chat.members).selectinload(ChatMember.user))
        .order_by(Chat.id.asc())
    )
    chats = list(chats_result.scalars().all())

    links_result = await session.execute(
        select(Link).where(Link.is_deleted == False).order_by(Link.id.asc())  # noqa: E712
    )
    links = list(links_result.scalars().all())

    chat_index_by_id: dict[int, int] = {c.id: i for i, c in enumerate(chats)}

    bundle: dict[str, Any] = {
        "version": 1,
        "generated_at": _dt_iso(datetime.now(timezone.utc)),
        "users": [
            {
                "public_id": u.public_id,
                "display_name": u.display_name,
                "public_key": base64.b64encode(u.public_key).decode("ascii"),
                "created_at": _dt_iso(u.created_at),
                "is_active": u.is_active,
            }
            for u in users
        ],
        "chats": [
            {
                "chat_type": c.chat_type.value,
                "title": c.title,
                "created_at": _dt_iso(c.created_at),
                "closed_at": _dt_iso(c.closed_at),
                "members": [
                    {
                        "public_id": m.user.public_id,
                        "joined_at": _dt_iso(m.joined_at),
                        "resume_hash": m.resume_hash,
                    }
                    for m in c.members
                    if m.user is not None
                ],
            }
            for c in chats
        ],
        "links": [
            {
                "token": link.token,
                "link_type": link.link_type.value,
                "chat_index": chat_index_by_id.get(link.chat_id)
                if link.chat_id is not None
                else None,
                "max_uses": link.max_uses,
                "uses_count": link.uses_count,
                "is_active": link.is_active,
                "password_required": link.password_hash is not None,
                "password_hash": link.password_hash,
                "failed_attempts": link.failed_attempts,
                "failed_window_started_at": _dt_iso(link.failed_window_started_at),
                "revoked_at": _dt_iso(link.revoked_at),
                "note": link.note,
                "created_at": _dt_iso(link.created_at),
                "expires_at": _dt_iso(link.expires_at),
            }
            for link in links
        ],
    }

    payload = json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8")
    filename = f"sideword-config-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    return Response(
        content=payload,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        # Accept both trailing Z and explicit offsets.
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@router.post("/import")
async def import_bundle(
    bundle: UploadFile = File(..., description="JSON produced by /admin/api/export"),
    replace: bool = False,
    admin: Admin = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    raw = await bundle.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc

    if not isinstance(data, dict) or data.get("version") != 1:
        raise HTTPException(status_code=400, detail="unsupported bundle version")

    # Validate before mutating anything. Never silently downgrade protected links.
    for raw_user in data.get("users", []):
        try:
            public_key = base64.b64decode(raw_user.get("public_key", ""), validate=True)
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, "Invalid participant P-256 public key.") from exc
        if not valid_public_key(public_key):
            raise HTTPException(400, "Invalid participant P-256 public key.")
    for raw_link in data.get("links", []):
        verifier = raw_link.get("password_hash")
        protected = verifier is not None or raw_link.get("password_required")
        if protected and not valid_verifier(verifier):
            raise HTTPException(400, "Invalid or missing room password verifier.")
    for raw_chat in data.get("chats", []):
        closed_at = raw_chat.get("closed_at")
        if closed_at is not None and (
            not isinstance(closed_at, str) or _parse_dt(closed_at) is None
        ):
            raise HTTPException(400, "Invalid conversation closure timestamp.")
        for raw_member in raw_chat.get("members", []):
            value = raw_member.get("resume_hash")
            if value is not None and (
                not isinstance(value, str) or len(value) != 64
                or any(c not in "0123456789abcdef" for c in value)
            ):
                raise HTTPException(400, "Invalid participant resume verifier.")

    # Replace mode wipes mutable tables while admins remain untouched.
    if replace:
        for model in (RefreshUse, ClientSession, SendRecord, ReadReceipt,
                      PendingMessage, ChatMember, Link, Chat, User):
            await session.execute(delete(model))
        await session.flush()

    # Users.
    users_by_public_id: dict[str, User] = {}
    result = await session.execute(select(User))
    for existing in result.scalars().all():
        users_by_public_id[existing.public_id] = existing

    for raw_user in data.get("users", []):
        pid = raw_user.get("public_id")
        if not pid:
            continue
        # All keys were validated before any replace-mode mutation.
        pub_key = base64.b64decode(raw_user["public_key"], validate=True)

        user = users_by_public_id.get(pid)
        if user is None:
            user = User(
                public_id=pid,
                display_name=raw_user.get("display_name"),
                public_key=pub_key,
                created_at=_parse_dt(raw_user.get("created_at"))
                or datetime.now(timezone.utc),
                is_active=bool(raw_user.get("is_active", True)),
            )
            session.add(user)
            await session.flush()
            users_by_public_id[pid] = user
        else:
            user.display_name = raw_user.get("display_name", user.display_name)
            user.public_key = pub_key
            user.is_active = bool(raw_user.get("is_active", user.is_active))

    # Chats.
    chats_out: list[Chat] = []
    for raw_chat in data.get("chats", []):
        chat_type = ChatType(raw_chat.get("chat_type", "personal"))
        chat = Chat(
            chat_type=chat_type,
            closed_at=_parse_dt(raw_chat.get("closed_at")),
            title=raw_chat.get("title"),
            created_at=_parse_dt(raw_chat.get("created_at"))
            or datetime.now(timezone.utc),
        )
        session.add(chat)
        await session.flush()
        chats_out.append(chat)

        for raw_member in raw_chat.get("members", []):
            pid = raw_member.get("public_id")
            user = users_by_public_id.get(pid) if pid else None
            if user is None:
                continue
            session.add(
                ChatMember(
                    chat_id=chat.id,
                    user_id=user.id,
                    joined_at=_parse_dt(raw_member.get("joined_at"))
                    or datetime.now(timezone.utc),
                    resume_hash=raw_member.get("resume_hash"),
                )
            )

    # Links.
    for raw_link in data.get("links", []):
        token = raw_link.get("token")
        if not token:
            continue
        chat_index = raw_link.get("chat_index")
        chat_id = None
        if isinstance(chat_index, int) and 0 <= chat_index < len(chats_out):
            chat_id = chats_out[chat_index].id
        link_type = LinkType(raw_link.get("link_type", "personal"))
        link = Link(
            token=token,
            link_type=link_type,
            chat_id=chat_id,
            max_uses=int(raw_link.get("max_uses") or (2 if link_type is LinkType.personal else 0)),
            uses_count=int(raw_link.get("uses_count") or 0),
            is_active=bool(raw_link.get("is_active", True)),
            password_hash=raw_link.get("password_hash"),
            failed_attempts=max(0, int(raw_link.get("failed_attempts") or 0)),
            failed_window_started_at=_parse_dt(raw_link.get("failed_window_started_at")),
            revoked_at=_parse_dt(raw_link.get("revoked_at")),
            note=raw_link.get("note"),
            created_at=_parse_dt(raw_link.get("created_at"))
            or datetime.now(timezone.utc),
            expires_at=_parse_dt(raw_link.get("expires_at")),
        )
        session.add(link)

    await session.commit()
    return JSONResponse(
        {
            "imported_users": len(data.get("users", [])),
            "imported_chats": len(data.get("chats", [])),
            "imported_links": len(data.get("links", [])),
            "replace": replace,
        }
    )
