"""Send/receive ciphertext messages and read receipts."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import SessionLocal, get_session
from ..delivery import (
    confirmation_available,
    delivery_ids,
    queued_receipts,
    receipt_payload,
    receipt_state,
    status_payload,
)
from ..deps import get_current_user
from ..models import (
    ChatMember,
    ChatType,
    PendingMessage,
    ReadReceipt,
    SendRecord,
    User,
)
from ..schemas import (
    AckRequest,
    ChatInfo,
    ExactAckRequest,
    ExactMarkReadRequest,
    IncomingMessage,
    MarkReadRequest,
    MessageReference,
    MessageStatusRequest,
    MessageStatusResponse,
    PollResponse,
    SendMessageRequest,
    SendMessageResponse,
    _decode_b64,
)
from ..services import chat_members, ensure_chat_member, load_chat_info
from ..ws_manager import manager as ws_manager

router = APIRouter(prefix="/api/v1", tags=["chats"])

_settings = get_settings()


@router.get("/chats/{chat_id}", response_model=ChatInfo)
async def get_chat(
    chat_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ChatInfo:
    chat = await ensure_chat_member(session, chat_id, user)
    return await load_chat_info(session, chat)


@router.post("/chats/{chat_id}/messages", response_model=SendMessageResponse)
async def send_message(
    chat_id: int,
    payload: SendMessageRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> SendMessageResponse:
    await session.execute(text("BEGIN IMMEDIATE"))
    chat = await ensure_chat_member(session, chat_id, user)
    now = datetime.now(timezone.utc)
    digest = hashlib.sha256(json.dumps(sorted(
        (env.recipient_public_id, hashlib.sha256(_decode_b64(env.ciphertext)).hexdigest())
        for env in payload.envelopes
    ), separators=(",", ":")).encode()).hexdigest()
    record = await session.scalar(select(SendRecord).where(
        SendRecord.chat_id == chat_id, SendRecord.sender_id == user.id,
        SendRecord.client_message_id == payload.client_message_id,
    ))
    if record and record.expires_at.replace(tzinfo=timezone.utc) > now:
        if record.payload_hash != digest:
            raise HTTPException(409, "message identity already has a different payload")
        return SendMessageResponse(client_message_id=payload.client_message_id,
                                   recipients=json.loads(record.recipients_json),
                                   deliveries=delivery_ids(record),
                                   created_at=record.created_at)
    if record:
        await session.delete(record)
        await session.flush()
    else:
        legacy = await session.scalar(select(PendingMessage.id).where(
            PendingMessage.chat_id == chat_id, PendingMessage.sender_id == user.id,
            PendingMessage.client_message_id == payload.client_message_id,
        ).limit(1))
        if legacy is not None:
            raise HTTPException(409, "message predates retry ledger; verify delivery")
    members = await chat_members(session, chat_id)
    by_pid = {m.public_id: m for m in members}

    if chat.chat_type is ChatType.personal and len(members) < 2:
        raise HTTPException(
            status_code=409,
            detail="personal chat has no peer yet",
        )

    seen_recipients: set[str] = set()
    recipients_users: list[User] = []
    ciphertexts: dict[int, bytes] = {}
    for env in payload.envelopes:
        if env.recipient_public_id == user.public_id:
            raise HTTPException(
                status_code=400, detail="cannot send envelope to self"
            )
        recipient = by_pid.get(env.recipient_public_id)
        if recipient is None:
            raise HTTPException(
                status_code=400,
                detail=f"unknown recipient {env.recipient_public_id}",
            )
        if env.recipient_public_id in seen_recipients:
            raise HTTPException(
                status_code=400,
                detail=f"duplicate envelope for {env.recipient_public_id}",
            )
        data = _decode_b64(env.ciphertext)
        if len(data) > _settings.max_ciphertext_bytes:
            raise HTTPException(status_code=413, detail="ciphertext too large")
        seen_recipients.add(env.recipient_public_id)
        recipients_users.append(recipient)
        ciphertexts[recipient.id] = data

    # Every chat member except the sender must receive an envelope.
    required = {m.public_id for m in members if m.id != user.id}
    if seen_recipients != required:
        missing = required - seen_recipients
        extras = seen_recipients - required
        raise HTTPException(
            status_code=400,
            detail={
                "error": "envelopes must cover exactly all peers",
                "missing": sorted(missing),
                "unexpected": sorted(extras),
            },
        )

    count, size = (await session.execute(select(
        func.count(PendingMessage.id),
        func.coalesce(func.sum(func.length(PendingMessage.ciphertext)), 0)
    ))).one()
    ledger_count = await session.scalar(select(func.count(SendRecord.id)))
    own_count, own_bytes = (await session.execute(select(
        func.count(PendingMessage.id),
        func.coalesce(func.sum(func.length(PendingMessage.ciphertext)), 0),
    ).where(PendingMessage.sender_id == user.id))).one()
    own_receipts = await session.scalar(select(func.count(ReadReceipt.id)).where(
        ReadReceipt.sender_id == user.id))
    recent = await session.scalar(select(func.count(SendRecord.id)).where(
        SendRecord.sender_id == user.id, SendRecord.created_at > now - timedelta(minutes=1)))
    if (count + len(recipients_users) > _settings.max_pending_messages
            or size + sum(map(len, ciphertexts.values())) > _settings.max_pending_bytes
            or ledger_count >= _settings.max_send_records
            or own_count + own_receipts + len(recipients_users) > _settings.max_pending_per_sender
            or (own_bytes + sum(map(len, ciphertexts.values()))
                > _settings.max_pending_bytes_per_sender)
            or recent >= _settings.sends_per_minute):
        raise HTTPException(429, "relay capacity reached; retry later",
                            headers={"Retry-After": "60"})
    record = SendRecord(
        chat_id=chat_id, sender_id=user.id, client_message_id=payload.client_message_id,
        payload_hash=digest, recipients_json=json.dumps([r.public_id for r in recipients_users]),
        created_at=now, expires_at=now + timedelta(days=_settings.send_idempotency_days),
    )
    session.add(record)
    new_rows = [
        PendingMessage(
            client_message_id=payload.client_message_id,
            chat_id=chat.id,
            sender_id=user.id,
            recipient_id=rec.id,
            ciphertext=ciphertexts[rec.id],
            created_at=now,
        )
        for rec in recipients_users
    ]
    session.add_all(new_rows)
    await session.flush()
    record.receipt_state_json = json.dumps({
        "sender_public_id": user.public_id,
        "recipients": {recipient.public_id: {
            "recipient_id": recipient.id, "delivery_id": row.delivery_id,
            "delivered_at": None, "read_at": None,
        } for recipient, row in zip(recipients_users, new_rows, strict=True)},
    })
    await session.commit()

    # Push immediately to online recipients.
    for row in new_rows:
        if ws_manager.is_online(row.recipient_id):
            await ws_manager.send_json(
                row.recipient_id,
                {
                    "type": "message",
                    "message": IncomingMessage(
                        id=row.id,
                        delivery_id=row.delivery_id,
                        client_message_id=row.client_message_id,
                        chat_id=row.chat_id,
                        sender_public_id=user.public_id,
                        ciphertext=base64.b64encode(row.ciphertext).decode("ascii"),
                        created_at=row.created_at,
                    ).model_dump(mode="json"),
                },
            )

    return SendMessageResponse(
        client_message_id=payload.client_message_id,
        recipients=[r.public_id for r in recipients_users],
        deliveries=delivery_ids(record),
        created_at=now,
    )


@router.post("/chats/{chat_id}/messages/status", response_model=MessageStatusResponse)
async def message_status(
    chat_id: int,
    payload: MessageStatusRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MessageStatusResponse:
    await ensure_chat_member(session, chat_id, user)
    records = await session.scalars(select(SendRecord).where(
        SendRecord.chat_id == chat_id, SendRecord.sender_id == user.id,
        SendRecord.client_message_id.in_(payload.client_message_ids),
    ))
    now = datetime.now(timezone.utc)
    return MessageStatusResponse(statuses=[status_payload(record) for record in records
        if confirmation_available(record, now)
        and receipt_state(record).get("sender_public_id", user.public_id) == user.public_id])


async def _fetch_poll(session: AsyncSession, user: User) -> PollResponse:
    # Pending ciphertext addressed to this user.
    result = await session.execute(
        select(PendingMessage, User)
        .join(User, User.id == PendingMessage.sender_id)
        .where(PendingMessage.recipient_id == user.id)
        .order_by(PendingMessage.id.asc()).limit(100)
    )
    pending_rows = result.all()

    to_mark = []
    now = datetime.now(timezone.utc)
    incoming: list[IncomingMessage] = []
    for msg, sender in pending_rows:
        incoming.append(
            IncomingMessage(
                id=msg.id,
                delivery_id=msg.delivery_id,
                client_message_id=msg.client_message_id,
                chat_id=msg.chat_id,
                sender_public_id=sender.public_id,
                ciphertext=base64.b64encode(msg.ciphertext).decode("ascii"),
                created_at=msg.created_at,
            )
        )
        if msg.delivered_at is None:
            to_mark.append(msg.delivery_id)

    incoming_receipts, delivery_receipts = await queued_receipts(session, user.id)

    if to_mark:
        await session.execute(update(PendingMessage).where(
            PendingMessage.delivery_id.in_(to_mark),
            PendingMessage.recipient_id == user.id,
        ).values(delivered_at=now))
        await session.commit()

    return PollResponse(messages=incoming, read_receipts=incoming_receipts,
                        delivery_receipts=delivery_receipts)


@router.get("/poll", response_model=PollResponse)
async def poll(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    _: int = Query(default=0, description="optional cache-buster"),
) -> PollResponse:
    return await _fetch_poll(session, user)


@router.post("/ack")
async def ack(
    payload: AckRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    """Confirm local persistence so rows can be deleted on the server."""

    if not _settings.allow_legacy_ack:
        raise HTTPException(410, "use /ack/exact")
    deleted_messages = 0
    deleted_receipts = 0
    if payload.message_ids:
        res = await session.execute(
            delete(PendingMessage).where(
                and_(
                    PendingMessage.recipient_id == user.id,
                    PendingMessage.id.in_(payload.message_ids),
                )
            )
        )
        deleted_messages = res.rowcount or 0
    if payload.read_ids:
        res = await session.execute(
            delete(ReadReceipt).where(
                and_(
                    ReadReceipt.sender_id == user.id,
                    ReadReceipt.id.in_(payload.read_ids),
                )
            )
        )
        deleted_receipts = res.rowcount or 0
    await session.commit()
    return {"deleted_messages": deleted_messages, "deleted_receipts": deleted_receipts}


@router.post("/chats/{chat_id}/read")
async def mark_read(
    chat_id: int,
    payload: MarkReadRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    """Mark inbound ciphertext as read.

    Rows are deleted on the server while senders receive receipts via poll/ws;
    receipts themselves are removed after the sender ACKs them.
    """

    if not _settings.allow_legacy_ack:
        raise HTTPException(410, "use /read/exact")
    chat = await ensure_chat_member(session, chat_id, user)

    # Match pending rows for this chat/recipient and the given client_message_ids.
    result = await session.execute(
        delete(PendingMessage)
        .where(
            PendingMessage.chat_id == chat.id,
            PendingMessage.recipient_id == user.id,
            PendingMessage.client_message_id.in_(payload.client_message_ids),
        )
        .returning(PendingMessage)
    )
    msgs = list(result.scalars().all())
    return await _finish_read(session, user, msgs)


def _message_match(ref: MessageReference):
    return and_(
        PendingMessage.delivery_id == ref.delivery_id,
        PendingMessage.chat_id == ref.chat_id,
        PendingMessage.client_message_id == ref.client_message_id,
        PendingMessage.sender_id.in_(select(User.id).where(User.public_id == ref.sender_public_id)),
    )


@router.post("/ack/exact")
async def ack_exact(
    payload: ExactAckRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    """Delete only the identified deliveries owned by the authenticated user."""
    messages = receipts = 0
    confirmations: list[ReadReceipt] = []
    if payload.confirm_delivery and payload.messages:
        await session.execute(text("BEGIN IMMEDIATE"))
    if payload.messages:
        statement = delete(PendingMessage).where(
            PendingMessage.recipient_id == user.id,
            or_(*(_message_match(ref) for ref in payload.messages)),
        )
        if payload.confirm_delivery:
            result = await session.execute(statement.returning(PendingMessage))
            rows = list(result.scalars().all())
            messages = len(rows)
            now = datetime.now(timezone.utc)
            for row in rows:
                await _record_delivery(session, user, row, now)
            # Queue pressure must not retain ciphertext after durable delivery.
            # The bounded ledger provides sender-only recovery for missed events.
            count = await session.scalar(select(func.count(ReadReceipt.id)))
            if count + len(rows) <= _settings.max_read_receipts:
                confirmations = [_confirmation(row, user, "delivered", now) for row in rows]
                session.add_all(confirmations)
        else:
            result = await session.execute(statement)
            messages = result.rowcount or 0
    if payload.receipts:
        result = await session.execute(delete(ReadReceipt).where(
            ReadReceipt.sender_id == user.id,
            or_(*(and_(
                ReadReceipt.delivery_id == ref.delivery_id,
                ReadReceipt.chat_id == ref.chat_id,
                ReadReceipt.client_message_id == ref.client_message_id,
                ReadReceipt.reader_public_id == ref.reader_public_id,
            ) for ref in payload.receipts)),
        ))
        receipts = result.rowcount or 0
    await session.flush()
    await session.commit()
    await _push_receipts(confirmations)
    return {"deleted_messages": messages, "deleted_receipts": receipts}


@router.post("/chats/{chat_id}/read/exact")
async def mark_read_exact(
    chat_id: int,
    payload: ExactMarkReadRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    if payload.viewed:
        await session.execute(text("BEGIN IMMEDIATE"))
    await ensure_chat_member(session, chat_id, user)
    if any(ref.chat_id != chat_id for ref in payload.messages):
        raise HTTPException(status_code=422, detail="message chat does not match route")
    if payload.viewed:
        return await _mark_viewed(session, user, payload.messages)
    # DELETE RETURNING makes row consumption atomic across concurrent tabs and
    # workers. Only the winner creates a receipt, in the same transaction.
    result = await session.execute(delete(PendingMessage).where(
        PendingMessage.recipient_id == user.id,
        or_(*(_message_match(ref) for ref in payload.messages)),
    ).returning(PendingMessage))
    return await _finish_read(session, user, list(result.scalars().all()))


async def _finish_read(
    session: AsyncSession, user: User, msgs: list[PendingMessage],
) -> dict[str, int]:
    if not msgs:
        await session.commit()
        return {"marked": 0}

    receipt_count = await session.scalar(select(func.count(ReadReceipt.id)))
    if receipt_count + len(msgs) > _settings.max_read_receipts:
        await session.rollback()
        raise HTTPException(429, "receipt capacity reached", headers={"Retry-After": "60"})
    now = datetime.now(timezone.utc)
    receipts: list[ReadReceipt] = []
    for msg in msgs:
        await _record_delivery(session, user, msg, now)
        receipts.append(_confirmation(msg, user, "read", now))

    session.add_all(receipts)
    await session.flush()
    await session.commit()

    await _push_receipts(receipts)
    return {"marked": len(msgs)}


def _confirmation(msg: PendingMessage, user: User, kind: str, now: datetime) -> ReadReceipt:
    return ReadReceipt(
        client_message_id=msg.client_message_id, chat_id=msg.chat_id, sender_id=msg.sender_id,
        reader_public_id=user.public_id, kind=kind, message_delivery_id=msg.delivery_id,
        created_at=now,
    )


async def _record_delivery(session, user, msg, now):
    record = await session.scalar(select(SendRecord).where(
        SendRecord.chat_id == msg.chat_id, SendRecord.sender_id == msg.sender_id,
        SendRecord.client_message_id == msg.client_message_id,
    ))
    if record is None or not confirmation_available(record, now):
        return
    sender = await session.get(User, msg.sender_id)
    state = receipt_state(record)
    if sender is None or state.get("sender_public_id", sender.public_id) != sender.public_id:
        return
    if user.public_id not in json.loads(record.recipients_json):
        return
    state.setdefault("sender_public_id", sender.public_id)
    recipients = state.setdefault("recipients", {})
    current = recipients.setdefault(user.public_id, {
        "recipient_id": user.id, "delivery_id": msg.delivery_id,
        "delivered_at": None, "read_at": None,
    })
    if current["recipient_id"] != user.id or current["delivery_id"] != msg.delivery_id:
        return
    current["delivered_at"] = current.get("delivered_at") or now.isoformat()
    record.receipt_state_json = json.dumps(state)


async def _mark_viewed(session, user, references):
    now = datetime.now(timezone.utc)
    receipts = []
    for ref in references:
        record = await session.scalar(select(SendRecord).where(
            SendRecord.chat_id == ref.chat_id,
            SendRecord.client_message_id == ref.client_message_id,
            SendRecord.sender_id.in_(select(User.id).where(User.public_id == ref.sender_public_id)),
        ))
        if record is None or not confirmation_available(record, now):
            continue
        state = receipt_state(record)
        current = state.get("recipients", {}).get(user.public_id)
        if (state.get("sender_public_id") != ref.sender_public_id or current is None
                or current.get("recipient_id") != user.id
                or current.get("delivery_id") != ref.delivery_id
                or not current.get("delivered_at") or current.get("read_at")):
            continue
        current["read_at"] = now.isoformat()
        record.receipt_state_json = json.dumps(state)
        receipts.append(ReadReceipt(
            chat_id=ref.chat_id, sender_id=record.sender_id, reader_public_id=user.public_id,
            client_message_id=ref.client_message_id, message_delivery_id=ref.delivery_id,
            kind="viewed", created_at=now,
        ))
    count = await session.scalar(select(func.count(ReadReceipt.id)))
    marked = len(receipts)
    if count + marked <= _settings.max_read_receipts:
        session.add_all(receipts)
    else:
        receipts = []  # Authoritative state remains recoverable without queue growth.
    await session.flush()
    await session.commit()
    await _push_receipts(receipts)
    return {"marked": marked}


async def _push_receipts(receipts: list[ReadReceipt]) -> None:
    for r in receipts:
        if ws_manager.is_online(r.sender_id):
            async with SessionLocal() as session:
                member = await session.scalar(select(ChatMember.user_id).where(
                    ChatMember.chat_id == r.chat_id, ChatMember.user_id == r.sender_id,
                ))
            if member is None:
                continue
            await ws_manager.send_json(
                r.sender_id,
                {
                    "type": "delivered" if r.kind == "delivered" else "read",
                    "delivery" if r.kind == "delivered" else "read":
                        receipt_payload(r).model_dump(mode="json"),
                },
            )



@router.delete("/chats/{chat_id}/outbox")
async def drop_undelivered(
    chat_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    """Drop this user's own unread ciphertext still queued on the server."""

    await ensure_chat_member(session, chat_id, user)
    res = await session.execute(
        delete(PendingMessage).where(
            PendingMessage.chat_id == chat_id,
            PendingMessage.sender_id == user.id,
        )
    )
    await session.commit()
    return {"deleted": res.rowcount or 0}
