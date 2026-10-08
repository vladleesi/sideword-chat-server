"""Bounded delivery metadata and receipt serialization; never message content."""

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .models import ChatMember, ReadReceipt, SendRecord
from .schemas import IncomingReadReceipt, MessageDeliveryStatus, RecipientDeliveryStatus


def utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def confirmation_available(record: SendRecord, now: datetime) -> bool:
    return now < max(
        utc(record.expires_at),
        utc(record.created_at) + timedelta(days=get_settings().message_ttl_days),
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


def status_payload(record: SendRecord) -> MessageDeliveryStatus:
    states = receipt_state(record).get("recipients", {})
    return MessageDeliveryStatus(
        client_message_id=record.client_message_id,
        created_at=record.created_at,
        recipients=[
            RecipientDeliveryStatus(
                public_id=pid,
                delivery_id=states.get(pid, {}).get("delivery_id"),
                delivered_at=states.get(pid, {}).get("delivered_at"),
                read_at=states.get(pid, {}).get("read_at"),
            )
            for pid in json.loads(record.recipients_json)
        ],
    )


async def queued_receipts(session: AsyncSession, user_id: int) -> tuple[list, list]:
    """Independent bounded queues keep legacy readers from being starved."""
    queues = []
    for delivered in (False, True):
        rows = await session.scalars(
            select(ReadReceipt)
            .where(
                ReadReceipt.sender_id == user_id,
                ReadReceipt.chat_id.in_(
                    select(ChatMember.chat_id).where(ChatMember.user_id == user_id)
                ),
                ReadReceipt.kind == "delivered" if delivered else ReadReceipt.kind != "delivered",
            )
            .order_by(ReadReceipt.id.asc())
            .limit(100)
        )
        queues.append([receipt_payload(row) for row in rows])
    return queues[0], queues[1]
