"""WebSocket endpoint for realtime ciphertext delivery and receipts."""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone

from anyio import CancelScope
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import SessionLocal
from ..delivery import queued_receipts
from ..deps import resolve_client_user
from ..models import PendingMessage, User
from ..schemas import IncomingMessage, IncomingReadReceipt
from ..ws_manager import manager

router = APIRouter()

_BASE_PROTOCOL = "sideword.v1"
_AUTH_PROTOCOL_PREFIX = "sideword.auth."
SESSION_CHECK_SECONDS = 30


def _token_from_subprotocol(websocket: WebSocket) -> str | None:
    protocols = websocket.headers.get("sec-websocket-protocol", "")
    for protocol in (item.strip() for item in protocols.split(",")):
        if protocol.startswith(_AUTH_PROTOCOL_PREFIX):
            return protocol.removeprefix(_AUTH_PROTOCOL_PREFIX)
    return None


async def _resolve_user(session: AsyncSession, token: str) -> User | None:
    return await resolve_client_user(session, token)


async def _backlog_payload(
    session: AsyncSession, user: User
) -> tuple[list[IncomingMessage], list[IncomingReadReceipt], list[IncomingReadReceipt]]:
    msg_res = await session.execute(
        select(PendingMessage, User)
        .join(User, User.id == PendingMessage.sender_id)
        .where(PendingMessage.recipient_id == user.id)
        .order_by(PendingMessage.id.asc()).limit(100)
    )
    messages = [
        IncomingMessage(
            id=msg.id,
            delivery_id=msg.delivery_id,
            client_message_id=msg.client_message_id,
            chat_id=msg.chat_id,
            sender_public_id=sender.public_id,
            ciphertext=base64.b64encode(msg.ciphertext).decode("ascii"),
            created_at=msg.created_at,
        )
        for msg, sender in msg_res.all()
    ]

    receipts, deliveries = await queued_receipts(session, user.id)
    return messages, receipts, deliveries


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket, token: str | None = Query(default=None)) -> None:
    auth_token = token or _token_from_subprotocol(websocket)
    requested_protocols = {
        item.strip()
        for item in websocket.headers.get("sec-websocket-protocol", "").split(",")
    }
    accepted_protocol = _BASE_PROTOCOL if _BASE_PROTOCOL in requested_protocols else None
    presence_requested = False

    # Browsers authenticate in the first frame so JWTs stay out of URLs and
    # WebSocket protocol headers. Query/subprotocol auth remains compatible.
    if auth_token is None:
        await websocket.accept(subprotocol=accepted_protocol)
        try:
            payload = await asyncio.wait_for(websocket.receive_json(), timeout=5)
        except (TimeoutError, ValueError, WebSocketDisconnect):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        if (
            not isinstance(payload, dict)
            or payload.get("type") != "auth"
            or not isinstance(payload.get("token"), str)
        ):
            await websocket.send_json({"type": "auth_error", "reason": "auth required"})
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        auth_token = payload["token"]
        presence_requested = payload.get("presence") is True

    connected_user_id: int | None = None
    try:
        async with SessionLocal() as session:
            user = await _resolve_user(session, auth_token)
            if user is None:
                if websocket.application_state.name == "CONNECTED":
                    await websocket.send_json({"type": "auth_error", "reason": "invalid session"})
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return
            user.last_seen_at = datetime.now(timezone.utc)
            await session.commit()

            if websocket.application_state.name != "CONNECTED":
                await websocket.accept(subprotocol=accepted_protocol)
            async def validate_session() -> bool:
                # A fresh session avoids stale ORM state after remote revocation.
                async with SessionLocal() as current:
                    return await _resolve_user(current, auth_token) is not None

            try:
                await manager.connect(
                    user.id, websocket, validate_session, public_id=user.public_id
                )
            except WebSocketDisconnect:
                return

            connected_user_id = user.id
            messages, receipts, deliveries = await _backlog_payload(session, user)

            # Backlog rows are treated as delivered once streamed down.
            if messages:
                await session.execute(update(PendingMessage).where(
                    PendingMessage.delivery_id.in_([m.delivery_id for m in messages]),
                    PendingMessage.recipient_id == user.id,
                    PendingMessage.delivered_at.is_(None),
                ).values(delivered_at=datetime.now(timezone.utc)))
                await session.commit()

            if not await manager.validate(user.id, websocket):
                return
            await websocket.send_json(
                {
                    "type": "hello",
                    "user": user.public_id,
                    "backlog": {
                        "messages": [m.model_dump(mode="json") for m in messages],
                        "read_receipts": [r.model_dump(mode="json") for r in receipts],
                        "delivery_receipts": [r.model_dump(mode="json") for r in deliveries],
                    },
                }
            )
            # Opt-in preserves the event stream for legacy clients.
            if presence_requested:
                await manager.enable_presence(websocket)
            else:
                await manager.refresh_presence()

        while True:
            # Clients may send ping frames; accept simple textual pings too.
            try:
                raw = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=min(SESSION_CHECK_SECONDS, manager.remaining(websocket)),
                )
            except TimeoutError:
                raw = None
            if not manager.remaining(websocket):
                await websocket.close(code=1001)
                return
            if not await manager.validate(user.id, websocket):
                return
            if raw is not None:
                manager.touch(websocket)
            if raw == "ping":
                await websocket.send_text("pong")
                await manager.refresh_presence(websocket)
    except WebSocketDisconnect:
        pass
    finally:
        # Cleanup covers cancellation during hello/presence setup as well as
        # the receive loop; a cancelled observer cannot abandon peer updates.
        if connected_user_id is not None:
            with CancelScope(shield=True):
                await manager.disconnect(connected_user_id, websocket)
