"""FastAPI dependencies: client and admin authentication."""

from __future__ import annotations

from datetime import datetime, timezone

import jwt
from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .db import get_session
from .message_keys import valid_public_key
from .models import Admin, AdminSession, ClientSession, Link, User
from .security import decode_admin_token, decode_client_token

ADMIN_COOKIE_NAME = "sideword_admin_session"


async def resolve_client_user(session: AsyncSession, token: str) -> User | None:
    """Resolve a JWT only while its user and issuing invite remain valid."""

    try:
        payload = decode_client_token(token)
        if payload.get("typ") != "client":
            return None
        user_id = int(payload.get("sub") or 0)
        link_id = int(payload.get("lid") or 0)
    except (jwt.InvalidTokenError, TypeError, ValueError):
        return None

    user = await session.get(User, user_id)
    link = await session.get(Link, link_id)
    if (
        user is None
        or not user.is_active
        or not valid_public_key(user.public_key)
        or user.public_id != payload.get("pid")
        or link is None
        or link.is_deleted
        or link.revoked_at is not None
    ):
        return None
    if link.expires_at is not None:
        expiry = link.expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry <= datetime.now(timezone.utc):
            return None
    now = datetime.now(timezone.utc)
    if "sid" in payload:
        record = await session.get(ClientSession, payload["sid"])
        if (record is None or record.revoked or record.user_id != user.id
                or record.public_id != user.public_id or record.link_id != link.id
                or record.expires_at.replace(tzinfo=timezone.utc) <= now):
            return None
    else:
        deadline = get_settings().legacy_token_deadline
        if deadline is not None and now >= deadline.replace(tzinfo=timezone.utc):
            return None
    return user


async def get_current_user(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    user = await resolve_client_user(session, token)
    if user is None:
        raise HTTPException(status_code=401, detail="invalid or revoked session")

    user.last_seen_at = datetime.now(timezone.utc)
    await session.commit()
    return user


async def get_current_admin(
    request: Request,
    session: AsyncSession = Depends(get_session),
    sideword_admin_session: str | None = Cookie(default=None, alias=ADMIN_COOKIE_NAME),
) -> Admin:
    token = sideword_admin_session
    if token is None:
        auth = request.headers.get("Authorization")
        if auth and auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="admin auth required")
    try:
        payload = decode_admin_token(token)
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=401, detail="session expired") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="invalid session") from exc

    if payload.get("typ") != "admin":
        raise HTTPException(status_code=401, detail="wrong token type")

    try:
        admin_id = int(payload.get("sub") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="invalid subject") from exc

    admin = await session.get(Admin, admin_id)
    record = await session.get(AdminSession, payload.get("sid", ""))
    if (admin is None or record is None or record.admin_id != admin_id
            or record.expires_at.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc)):
        raise HTTPException(401, "admin session expired or revoked")
    return admin


async def get_optional_admin(
    request: Request,
    session: AsyncSession = Depends(get_session),
    sideword_admin_session: str | None = Cookie(default=None, alias=ADMIN_COOKIE_NAME),
) -> Admin | None:
    if not sideword_admin_session:
        return None
    try:
        return await get_current_admin(
            request=request,
            session=session,
            sideword_admin_session=sideword_admin_session,
        )
    except HTTPException:
        return None


def require_admin_ui(request: Request) -> None:
    """Placeholder dependency for UI routes (real guard uses redirects)."""

    # Documentation-only stub.
    _ = request
