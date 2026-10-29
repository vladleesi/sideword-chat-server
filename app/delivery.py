"""Bounded delivery metadata and receipt serialization; never message content."""

import base64
import json
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import PendingMessage, ReadReceipt, SendRecord, User
from .schemas import (
    IncomingMessage,
    IncomingReadReceipt,
    MessageDeliveryStatus,
    RecipientDeliveryStatus,
)
from .services import open_chat_ids


def utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def confirmation_available(record: SendRecord, now: datetime) -> bool:
    return now < max(
        utc(record.expires_at),
        utc(record.content_expires_at),
    )


def receipt_state(record: SendRecord) -> dict:
    state = json.loads(record.receipt_state_json)
    return state if isinstance(state, dict) else {}


def delivery_ids(record: SendRecord) -> dict[str, str]:
    return {
        pid: value["delivery_id"]
        for pid, value in receipt_state(record).get("recipients", {}).items()
        if value.get("delivery_id")
    }


def receipt_payload(receipt: ReadReceipt) -> IncomingReadReceipt:
    return IncomingReadReceipt(
        id=receipt.id,
        delivery_id=receipt.delivery_id,
        chat_id=receipt.chat_id,
        client_message_id=receipt.client_message_id,
        reader_public_id=receipt.reader_public_id,
        message_delivery_id=receipt.message_delivery_id,
        view_confirmed=receipt.kind == "viewed",
        created_at=receipt.created_at,
    )


def status_payload(record: SendRecord, queued: set[str], now: datetime) -> MessageDeliveryStatus:
    states = receipt_state(record).get("recipients", {})
    return MessageDeliveryStatus(
        client_message_id=record.client_message_id,
        created_at=record.created_at,
        content_expires_at=record.content_expires_at,
        recipients=[
            RecipientDeliveryStatus(
                public_id=pid,
                delivery_id=states.get(pid, {}).get("delivery_id"),
                delivered_at=states.get(pid, {}).get("delivered_at"),
                read_at=states.get(pid, {}).get("read_at"),
                state=("delivered" if states.get(pid, {}).get("delivered_at")
                       else "expired" if now >= utc(record.content_expires_at)
                       else "pending" if states.get(pid, {}).get("delivery_id") in queued
                       else "unavailable"),
            )
            for pid in json.loads(record.recipients_json)
        ],
    )


async def queued_receipts(session: AsyncSession, user_id: int) -> tuple[list, list]:
    """Bound delivery and viewing queues independently so neither is starved."""
    queues = []
    for delivered in (False, True):
        rows = await session.scalars(
            select(ReadReceipt)
            .where(
                ReadReceipt.sender_id == user_id,
                ReadReceipt.chat_id.in_(open_chat_ids(user_id)),
                ReadReceipt.kind == ("delivered" if delivered else "viewed"),
            )
            .order_by(ReadReceipt.id.asc())
            .limit(100)
        )
        queues.append([receipt_payload(row) for row in rows])
    return queues[0], queues[1]


# Wire budget includes per-message metadata; receipts retain independent count caps.
MESSAGE_BATCH_BYTES = 3 * 1024 * 1024


async def queued_messages(session: AsyncSession, user_id: int) -> list[IncomingMessage]:
    now = datetime.now(timezone.utc)
    sizes = await session.execute(select(
        PendingMessage.delivery_id, func.length(PendingMessage.ciphertext),
    ).where(PendingMessage.recipient_id == user_id, PendingMessage.expires_at > now,
            PendingMessage.chat_id.in_(open_chat_ids(user_id)))
      .order_by(PendingMessage.id.asc()).limit(100))
    ids = []
    used = 0
    for delivery_id, size in sizes:
        used += 4 * ((size + 2) // 3) + 1024
        if used > MESSAGE_BATCH_BYTES:
            break
        ids.append(delivery_id)
    if not ids:
        return []
    # Exact identities prevent a deleted/reused SQLite row ID bypassing the byte budget.
    rows = await session.execute(select(PendingMessage, User)
        .join(User, User.id == PendingMessage.sender_id)
        .where(PendingMessage.delivery_id.in_(ids), PendingMessage.recipient_id == user_id,
               PendingMessage.expires_at > now,
               PendingMessage.chat_id.in_(open_chat_ids(user_id)))
        .order_by(PendingMessage.id.asc()))
    return [IncomingMessage(
        id=msg.id, delivery_id=msg.delivery_id, client_message_id=msg.client_message_id,
        chat_id=msg.chat_id, sender_public_id=sender.public_id,
        ciphertext=base64.b64encode(msg.ciphertext).decode("ascii"), created_at=msg.created_at,
    ) for msg, sender in rows]
