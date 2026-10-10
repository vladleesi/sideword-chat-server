"""Client-facing invite-link activation."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..deps import resolve_client_user
from ..invite_security import require_secure_transport, resume_digest
from ..models import User
from ..schemas import LinkActivateRequest, LinkActivateResponse, _decode_b64
from ..services import activate_link, load_chat_info, user_to_participant
from .sessions import digest, issue

router = APIRouter(prefix="/api/v1/links", tags=["links"])


async def _current_user_if_any(
    session: AsyncSession,
    authorization: str | None,
) -> User | None:
    if authorization is None:
        return None
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "invalid or revoked session")
    token = authorization.split(" ", 1)[1].strip()
    user = await resolve_client_user(session, token)
    if user is None:
        raise HTTPException(401, "invalid or revoked session")
    return user


@router.post("/activate", response_model=LinkActivateResponse)
async def activate(
    payload: LinkActivateRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> LinkActivateResponse:
    # Even unprotected invites exchange bearer credentials. Never let a
    # development toggle expose them.
    require_secure_transport(request, "Invite activation requires HTTPS (or local loopback).")
    resume_digest(
        payload.resume_credential.get_secret_value()
        if payload.resume_credential is not None else None,
    )
    digest(payload.session_credential.get_secret_value())
    # SELECT FOR UPDATE is ignored by SQLite. Acquire its write reservation
    # before any auth/membership reads, also across multiple server processes.
    await session.execute(text("BEGIN IMMEDIATE"))
    existing = await _current_user_if_any(session, authorization)

    pub_key = _decode_b64(payload.public_key)
    if existing is not None and pub_key != existing.public_key:
        raise HTTPException(
            status_code=409,
            detail="public_key mismatch with existing session",
        )

    user, chat, link = await activate_link(
        session=session,
        link_token=payload.token.get_secret_value(),
        public_key=pub_key,
        display_name=payload.display_name,
        current_user=existing,
        password=payload.password.get_secret_value() if payload.password is not None else None,
        resume_credential=(
            payload.resume_credential.get_secret_value()
            if payload.resume_credential is not None else None
        ),
    )

    chat_info = await load_chat_info(session, chat)
    await session.commit()
    credentials = await issue(
        session, user, link.id, payload.session_credential.get_secret_value(),
        authorization_token=authorization.split(" ", 1)[1].strip()
        if authorization is not None else None,
    )
    return LinkActivateResponse(
        **credentials,
        user=user_to_participant(user),
        chat=chat_info,
    )
