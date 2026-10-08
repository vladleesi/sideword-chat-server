"""Landing pages.

Route ``/l/{token}`` explains an invite link and links to the bundled test client.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_session
from ..models import Chat, Link, LinkType
from ..templates import templates

router = APIRouter(tags=["landing"])

_settings = get_settings()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {"public_url": _settings.public_url},
    )


@router.get("/l/{token}", response_class=HTMLResponse)
async def landing(
    token: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    result = await session.execute(select(Link).where(Link.token == token))
    link = result.scalar_one_or_none()

    status = "unknown"
    details: dict = {}
    if link is not None:
        expired = False
        if link.expires_at is not None:
            exp = link.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            expired = exp <= datetime.now(timezone.utc)
        if link.is_deleted or link.revoked_at is not None:
            status = "revoked"
        elif expired:
            status = "expired"
        elif link.chat_id is not None and (
            chat := await session.get(Chat, link.chat_id)
        ) is not None and chat.closed_at is not None:
            status = "closed"
        elif link.max_uses and link.uses_count >= link.max_uses:
            status = "used"
        elif not link.is_active:
            status = "used"
        else:
            status = "active"
        details = {
            "type": link.link_type.value,
            "is_personal": link.link_type is LinkType.personal,
            "uses_count": link.uses_count,
            "max_uses": link.max_uses,
            "expires_at": link.expires_at,
            "password_required": link.password_hash is not None,
        }

    return templates.TemplateResponse(
        request,
        "landing.html",
        {
            "token": token,
            "status": status,
            "details": details,
            "public_url": _settings.public_url,
        },
    )
