"""Authenticated client profile endpoints."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_session
from ..deps import get_current_user
from ..models import ClientSession, Link, User
from ..schemas import ChatInfo, MeResponse
from ..security import decode_client_token
from ..services import get_user_chats, load_chat_info, user_to_participant

router = APIRouter(prefix="/api/v1", tags=["me"])


@router.get("/me", response_model=MeResponse)
async def me(
    authorization: str = Header(...),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MeResponse:
    chats = await get_user_chats(session, user)
    info: list[ChatInfo] = [await load_chat_info(session, c) for c in chats]
    payload = decode_client_token(authorization.split(" ", 1)[1].strip())
    link = await session.get(Link, int(payload["lid"]))
    expiry = datetime.fromtimestamp(payload["exp"], timezone.utc)
    if link and link.expires_at:
        expiry = min(expiry, link.expires_at.replace(tzinfo=timezone.utc))
    if expiry is not None:
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        expiry = min(expiry, datetime.fromtimestamp(payload["exp"], timezone.utc))
    session_expiry = expiry
    if payload.get("sid"):
        record = await session.get(ClientSession, payload["sid"])
        if record is None:
            raise HTTPException(401, "session unavailable")
        session_expiry = record.expires_at.replace(tzinfo=timezone.utc)
        if link and link.expires_at:
            session_expiry = min(session_expiry, link.expires_at.replace(tzinfo=timezone.utc))
        expiry = min(expiry, session_expiry)
    return MeResponse(
        user=user_to_participant(user),
        chats=info,
        access_expires_at=expiry,
        session_expires_at=session_expiry,
        server_time=datetime.now(timezone.utc),
        send_retry_window_seconds=get_settings().send_idempotency_days * 86400,
        receipt_retention_seconds=max(
            get_settings().send_idempotency_days, get_settings().message_ttl_days
        ) * 86400,
    )
