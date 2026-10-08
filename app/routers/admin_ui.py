"""Admin HTML UI routes."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.concurrency import run_in_threadpool

from ..config import get_settings
from ..db import get_session
from ..deps import get_current_admin, get_optional_admin
from ..invite_security import hash_room_password, require_secure_transport, validate_password
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
from ..security import generate_link_token, public_key_fingerprint
from ..services import _link_expired
from ..templates import templates
from ..ws_manager import manager as ws_manager

router = APIRouter(prefix="/admin", tags=["admin-ui"])

_settings = get_settings()


def _require(admin: Admin | None) -> Admin:
    if admin is None:
        raise HTTPException(
            status_code=303,
            detail="redirect",
            headers={"Location": "/admin/login"},
        )
    return admin


async def _redirect_if_anonymous(admin: Admin | None) -> Admin | RedirectResponse:
    if admin is None:
        return RedirectResponse(url="/admin/login", status_code=303)
    return admin


def _link_url(link: Link) -> str:
    base = _settings.public_url.rstrip("/")
    return f"{base}/l/{link.token}"


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    admin: Admin | None = Depends(get_optional_admin),
    session: AsyncSession = Depends(get_session),
):
    if admin is None:
        return RedirectResponse(url="/admin/login", status_code=303)

    users_count = (await session.execute(select(func.count(User.id)))).scalar_one()
    active_links = (
        await session.execute(select(func.count(Link.id)).where(Link.is_active == True))  # noqa: E712
    ).scalar_one()
    chats_count = (await session.execute(select(func.count(Chat.id)))).scalar_one()
    pending_msgs = (
        await session.execute(select(func.count(PendingMessage.id)))
    ).scalar_one()

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "admin": admin,
            "stats": {
                "users": users_count,
                "links_active": active_links,
                "chats": chats_count,
                "pending_messages": pending_msgs,
            },
        },
    )


@router.get("/users", response_class=HTMLResponse)
async def users_list(
    request: Request,
    admin: Admin | None = Depends(get_optional_admin),
    session: AsyncSession = Depends(get_session),
):
    if admin is None:
        return RedirectResponse(url="/admin/login", status_code=303)

    result = await session.execute(
        select(User)
        .options(selectinload(User.memberships))
        .order_by(User.created_at.desc())
    )
    users = list(result.scalars().all())
    rows = []
    for u in users:
        rows.append(
            {
                "id": u.id,
                "public_id": u.public_id,
                "display_name": u.display_name,
                "fingerprint": public_key_fingerprint(u.public_key),
                "created_at": u.created_at,
                "last_seen_at": u.last_seen_at,
                "is_active": u.is_active,
                "chats": [m.chat_id for m in u.memberships],
            }
        )
    return templates.TemplateResponse(
        request,
        "users.html",
        {"admin": admin, "users": rows},
    )


@router.post("/users/{user_id}/deactivate")
async def deactivate_user(
    user_id: int,
    admin: Admin = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    user.is_active = False
    await session.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/users/{user_id}/activate")
async def activate_user(
    user_id: int,
    admin: Admin = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    user.is_active = True
    await session.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.get("/links", response_class=HTMLResponse)
async def links_list(
    request: Request,
    admin: Admin | None = Depends(get_optional_admin),
    session: AsyncSession = Depends(get_session),
):
    if admin is None:
        return RedirectResponse(url="/admin/login", status_code=303)

    result = await session.execute(
        select(Link).where(Link.is_deleted == False).order_by(Link.created_at.desc())  # noqa: E712
    )
    items = list(result.scalars().all())
    rows = [
        {
            "id": link.id,
            "token": link.token,
            "link_type": link.link_type.value,
            "chat_id": link.chat_id,
            "max_uses": link.max_uses,
            "uses_count": link.uses_count,
            "is_expired": _link_expired(link),
            "is_revoked": link.revoked_at is not None,
            "is_full": bool(link.max_uses and link.uses_count >= link.max_uses),
            "password_required": link.password_hash is not None,
            "is_active": link.is_active and (
                link.expires_at is None
                or (
                    link.expires_at
                    if link.expires_at.tzinfo
                    else link.expires_at.replace(tzinfo=timezone.utc)
                )
                > datetime.now(timezone.utc)
            ),
            "note": link.note,
            "created_at": link.created_at,
            "expires_at": link.expires_at,
            "url": _link_url(link),
        }
        for link in items
    ]
    return templates.TemplateResponse(
        request,
        "links.html",
        {"admin": admin, "links": rows, "public_url": _settings.public_url,
         "form_values": getattr(request.state, "form_values", {}),
         "link_error": getattr(request.state, "link_error", None),
         "created_invite": getattr(request.state, "created_invite", None),
         "field_errors": getattr(request.state, "field_errors", {})},
        status_code=422 if getattr(request.state, "field_errors", {}) else 200,
    )


@router.post("/links")
async def create_link(
    request: Request,
    link_type: str = Form(...),
    note: str | None = Form(default=None),
    expires_in_hours: str | None = Form(default=None),
    group_title: str | None = Form(default=None),
    password_mode: str = Form(default="none"),
    room_password: str = Form(default=""),
    participant_limit: str = Form(default=""),
    admin: Admin = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if link_type not in ("personal", "group"):
        raise HTTPException(status_code=400, detail="invalid link type")
    lt = LinkType(link_type)

    request.state.form_values = {
        "link_type": link_type, "note": note or "", "group_title": group_title or "",
        "expires_in_hours": expires_in_hours or "", "password_mode": password_mode,
        "participant_limit": participant_limit,
    }
    errors = {}
    password = None
    if password_mode == "generated":
        password = secrets.token_urlsafe(12)
    elif password_mode == "custom":
        password = room_password
        try:
            validate_password(password)
        except ValueError as exc:
            errors["room_password"] = str(exc)
    elif password_mode != "none":
        errors["room_password"] = "Choose no password, generated password, or custom phrase."
    limit = 2 if lt is LinkType.personal else 0
    if lt is LinkType.group and participant_limit.strip():
        try:
            limit = int(participant_limit)
            if not 2 <= limit <= 1000:
                raise ValueError
        except ValueError:
            errors["participant_limit"] = "Enter a whole number from 2 to 1000, or leave blank."
    if errors:
        request.state.field_errors = errors
        return await links_list(request, admin, session)
    if password is not None:
        require_secure_transport(request)

    expires_at = None
    expiry = (expires_in_hours or "").strip()
    if expiry:
        try:
            hours = int(expiry)
            if not 1 <= hours <= 8760:
                raise ValueError
        except ValueError:
            request.state.field_errors = {
                "expires_in_hours": "Enter a whole number from 1 to 8760.",
            }
            return await links_list(request, admin, session)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=hours)

    link = Link(
        token=generate_link_token(),
        link_type=lt,
        max_uses=limit,
        password_hash=await run_in_threadpool(hash_room_password, password)
        if password is not None else None,
        uses_count=0,
        is_active=True,
        note=(note or None),
        expires_at=expires_at,
    )

    if lt is LinkType.group:
        chat = Chat(chat_type=ChatType.group, title=(group_title or None))
        session.add(chat)
        await session.flush()
        link.chat_id = chat.id

    session.add(link)
    await session.commit()
    if password is not None:
        request.state.created_invite = {"url": _link_url(link), "password": password}
        request.state.form_values = {}
        return await links_list(request, admin, session)
    return RedirectResponse(url="/admin/links", status_code=303)


@router.post("/links/{link_id}/revoke")
async def revoke_link(
    link_id: int,
    admin: Admin = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    link = await session.get(Link, link_id)
    if link is None or link.is_deleted:
        raise HTTPException(status_code=404, detail="link not found")
    link.is_active = False
    link.revoked_at = datetime.now(timezone.utc)
    user_ids: set[int] = set()
    if link.chat_id is not None:
        result = await session.execute(
            select(ChatMember.user_id).where(ChatMember.chat_id == link.chat_id)
        )
        user_ids = set(result.scalars().all())
    await session.commit()
    await ws_manager.revoke(user_ids)
    return RedirectResponse(url="/admin/links", status_code=303)


@router.post("/links/{link_id}/reactivate")
async def reactivate_link(
    link_id: int,
    request: Request,
    admin: Admin = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    link = await session.get(Link, link_id)
    if link is None or link.is_deleted:
        raise HTTPException(status_code=404, detail="link not found")
    if _link_expired(link):
        request.state.link_error = "This link has expired. Create a new link."
        return await links_list(request, admin, session)
    # Restore existing access without opening a full personal chat to new users.
    link.is_active = not (link.max_uses and link.uses_count >= link.max_uses)
    link.revoked_at = None
    await session.commit()
    return RedirectResponse(url="/admin/links", status_code=303)


async def _delete_users(session: AsyncSession, users: list[User]) -> None:
    ids = {user.id for user in users}
    pids = {user.public_id for user in users}
    if not ids:
        return
    await session.execute(delete(PendingMessage).where(or_(
        PendingMessage.sender_id.in_(ids), PendingMessage.recipient_id.in_(ids),
    )))
    await session.execute(delete(ReadReceipt).where(or_(
        ReadReceipt.sender_id.in_(ids), ReadReceipt.reader_public_id.in_(pids),
    )))
    await session.execute(delete(ChatMember).where(ChatMember.user_id.in_(ids)))
    await session.execute(delete(SendRecord).where(SendRecord.sender_id.in_(ids)))
    await session.execute(delete(RefreshUse).where(RefreshUse.session_id.in_(
        select(ClientSession.id).where(ClientSession.user_id.in_(ids)))))
    await session.execute(delete(ClientSession).where(ClientSession.user_id.in_(ids)))
    await session.execute(delete(User).where(User.id.in_(ids)))
    await session.commit()
    await ws_manager.revoke(ids)


async def _delete_links(
    session: AsyncSession, links: list[Link], *, commit: bool = True,
) -> set[int]:
    chat_ids = {link.chat_id for link in links if link.chat_id is not None}
    users = await session.scalars(select(ChatMember.user_id).where(
        ChatMember.chat_id.in_(chat_ids)
    ))
    user_ids = set(users.all())
    for link in links:
        # Keep only a tombstone ID: SQLite must never reuse a JWT's issuing
        # link ID. Remove the invite token, note and chat association.
        link.is_deleted = True
        link.is_active = False
        link.revoked_at = datetime.now(timezone.utc)
        link.token = generate_link_token()
        link.note = None
        link.password_hash = None
        link.failed_attempts = 0
        link.failed_window_started_at = None
        link.chat_id = None
    if commit:
        await session.commit()
        await ws_manager.revoke(user_ids)
    return user_ids


async def _delete_chats(session: AsyncSession, chat_ids: set[int]) -> None:
    if not chat_ids:
        return
    users = set((await session.scalars(
        select(ChatMember.user_id).where(ChatMember.chat_id.in_(chat_ids))
    )).all())
    links = list((await session.scalars(
        select(Link).where(Link.chat_id.in_(chat_ids))
    )).all())
    await _delete_links(session, links, commit=False)
    # Apply the link tombstones before deleting chats, including on databases
    # that enforce foreign keys. All mutations belong to one transaction.
    await session.flush()
    await session.execute(delete(PendingMessage).where(PendingMessage.chat_id.in_(chat_ids)))
    await session.execute(delete(ReadReceipt).where(ReadReceipt.chat_id.in_(chat_ids)))
    await session.execute(delete(ChatMember).where(ChatMember.chat_id.in_(chat_ids)))
    await session.execute(delete(SendRecord).where(SendRecord.chat_id.in_(chat_ids)))
    await session.execute(delete(Chat).where(Chat.id.in_(chat_ids)))
    await session.commit()
    await ws_manager.revoke(users)


@router.post("/{resource}/delete-selected")
async def delete_selected(
    resource: str,
    ids: list[int] = Form(...),
    confirm: str = Form(...),
    admin: Admin = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    if confirm != "delete":
        raise HTTPException(status_code=400, detail="confirmation required")
    selected = set(ids)
    if not selected or len(selected) > 500:
        raise HTTPException(status_code=400, detail="select between 1 and 500 items")
    if resource == "users":
        users = await session.scalars(select(User).where(User.id.in_(selected)))
        await _delete_users(session, list(users.all()))
    elif resource == "links":
        links = await session.scalars(select(Link).where(
            Link.id.in_(selected), Link.is_deleted == False,  # noqa: E712
        ))
        await _delete_links(session, list(links.all()))
    elif resource == "chats":
        await _delete_chats(session, selected)
    else:
        raise HTTPException(status_code=404, detail="unknown resource")
    return RedirectResponse(f"/admin/{resource}", status_code=303)


@router.post("/chats/{chat_id}/delete")
async def delete_chat(
    chat_id: int,
    confirm: str = Form(...),
    admin: Admin = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    if confirm != "delete":
        raise HTTPException(status_code=400, detail="confirmation required")
    if await session.get(Chat, chat_id) is None:
        raise HTTPException(status_code=404, detail="chat not found")
    await _delete_chats(session, {chat_id})
    return RedirectResponse("/admin/chats", status_code=303)


@router.post("/users/{user_id}/delete")
async def delete_user(
    user_id: int,
    confirm: str = Form(...),
    admin: Admin = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    if confirm != "delete":
        raise HTTPException(status_code=400, detail="confirmation required")
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    await _delete_users(session, [user])
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/cleanup")
async def cleanup_users(
    confirm: str = Form(...),
    admin: Admin = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    if confirm != "delete":
        raise HTTPException(status_code=400, detail="confirmation required")
    users = await session.scalars(select(User).where(User.is_active == False))  # noqa: E712
    await _delete_users(session, list(users.all()))
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/links/{link_id}/delete")
async def delete_link(
    link_id: int,
    confirm: str = Form(...),
    admin: Admin = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    if confirm != "delete":
        raise HTTPException(status_code=400, detail="confirmation required")
    link = await session.get(Link, link_id)
    if link is None or link.is_deleted:
        raise HTTPException(status_code=404, detail="link not found")
    await _delete_links(session, [link])
    return RedirectResponse("/admin/links", status_code=303)


@router.post("/links/cleanup")
async def cleanup_links(
    confirm: str = Form(...),
    admin: Admin = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    if confirm != "delete":
        raise HTTPException(status_code=400, detail="confirmation required")
    links = await session.scalars(select(Link).where(Link.is_deleted == False))  # noqa: E712
    await _delete_links(session, [
        link for link in links if link.revoked_at is not None or _link_expired(link)
    ])
    return RedirectResponse("/admin/links", status_code=303)


@router.get("/chats", response_class=HTMLResponse)
async def chats_list(
    request: Request,
    admin: Admin | None = Depends(get_optional_admin),
    session: AsyncSession = Depends(get_session),
):
    if admin is None:
        return RedirectResponse(url="/admin/login", status_code=303)

    result = await session.execute(
        select(Chat)
        .options(selectinload(Chat.members).selectinload(ChatMember.user))
        .order_by(Chat.created_at.desc())
    )
    chats = list(result.scalars().all())

    pending_counts_result = await session.execute(
        select(PendingMessage.chat_id, func.count(PendingMessage.id))
        .group_by(PendingMessage.chat_id)
    )
    pending_by_chat = dict(pending_counts_result.all())

    rows = [
        {
            "id": c.id,
            "chat_type": c.chat_type.value,
            "title": c.title,
            "closed_at": c.closed_at,
            "created_at": c.created_at,
            "members": [m.user.public_id for m in c.members if m.user is not None],
            "pending": pending_by_chat.get(c.id, 0),
        }
        for c in chats
    ]
    return templates.TemplateResponse(
        request,
        "chats.html",
        {"admin": admin, "chats": rows},
    )


@router.post("/chats/{chat_id}/close")
@router.post("/chats/{chat_id}/reopen")
async def set_chat_state(
    chat_id: int,
    request: Request,
    admin: Admin = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    # Serialize closure with sends, including across independent workers.
    await session.execute(text("BEGIN IMMEDIATE"))
    chat = await session.get(Chat, chat_id)
    if chat is None:
        raise HTTPException(404, "chat not found")
    if request.url.path.endswith("/close"):
        if chat.closed_at is None:
            chat.closed_at = datetime.now(timezone.utc)
    else:
        chat.closed_at = None
    await session.commit()
    await ws_manager.notify_chat_state(chat_id)
    return RedirectResponse("/admin/chats", status_code=303)


@router.post("/chats/{chat_id}/purge")
async def purge_chat(
    chat_id: int,
    admin: Admin = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    from sqlalchemy import delete

    await session.execute(
        delete(PendingMessage).where(PendingMessage.chat_id == chat_id)
    )
    await session.commit()
    return RedirectResponse(url="/admin/chats", status_code=303)


@router.get("/export-ui", response_class=HTMLResponse)
async def export_ui(
    request: Request,
    admin: Admin | None = Depends(get_optional_admin),
):
    if admin is None:
        return RedirectResponse(url="/admin/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "export.html",
        {"admin": admin},
    )
