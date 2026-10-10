"""Pydantic schemas for HTTP and WebSocket payloads."""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .message_keys import valid_public_key


def _decode_b64(value: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError("expected a base64 string")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"invalid base64: {exc}") from exc


def _encode_b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


class Base64Field(str):
    """Marker annotation: value is base64-encoded binary."""


class LinkActivateRequest(BaseModel):
    token: SecretStr = Field(..., min_length=1, max_length=128,
                             description="Invite admission secret; never log request bodies.")
    public_key: str = Field(..., max_length=88,
                            description="Base64 uncompressed P-256 public point (65 bytes).")
    display_name: str | None = Field(default=None, max_length=64)
    password: SecretStr | None = None
    resume_credential: SecretStr | None = None
    session_credential: SecretStr

    @field_validator("public_key")
    @classmethod
    def _check_public_key(cls, value: str) -> str:
        data = _decode_b64(value)
        if not valid_public_key(data):
            raise ValueError("public_key must be a valid uncompressed P-256 point (65 bytes)")
        return value


class ParticipantInfo(BaseModel):
    public_id: str
    public_key: str = Field(..., description="base64")
    display_name: str | None = None
    key_fingerprint: str


class ChatInfo(BaseModel):
    id: int
    chat_type: Literal["personal", "group"]
    title: str | None = None
    created_at: datetime
    closed_at: datetime | None = None
    participants: list[ParticipantInfo]


class LinkActivateResponse(BaseModel):
    token: str = Field(..., description="Client JWT; send as Authorization: Bearer.")
    session_id: str
    access_expires_at: datetime
    session_expires_at: datetime
    user: ParticipantInfo
    chat: ChatInfo


class MeResponse(BaseModel):
    user: ParticipantInfo
    chats: list[ChatInfo]
    access_expires_at: datetime | None = None
    server_time: datetime | None = None
    send_retry_window_seconds: int = 0
    receipt_retention_seconds: int = 0
    session_expires_at: datetime | None = None


class MessageEnvelope(BaseModel):
    recipient_public_id: str = Field(..., description="Recipient public_id.")
    ciphertext: str = Field(..., description="Base64 ciphertext (nonce + MAC included).")

    @field_validator("ciphertext")
    @classmethod
    def _ciphertext_is_b64(cls, value: str) -> str:
        _decode_b64(value)
        return value


class SendMessageRequest(BaseModel):
    client_message_id: str = Field(..., min_length=1, max_length=64)
    envelopes: list[MessageEnvelope] = Field(..., min_length=1, max_length=100)


class TimestampedPayload(BaseModel):
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        # SQLite returns naive datetimes even for timezone-aware columns.
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class SendMessageResponse(TimestampedPayload):
    client_message_id: str
    recipients: list[str]
    deliveries: dict[str, str] = Field(default_factory=dict)
    created_at: datetime


class IncomingMessage(TimestampedPayload):
    id: int
    delivery_id: str
    client_message_id: str
    chat_id: int
    sender_public_id: str
    ciphertext: str = Field(..., description="Base64 ciphertext.")
    created_at: datetime


class IncomingReadReceipt(TimestampedPayload):
    id: int
    delivery_id: str
    client_message_id: str
    chat_id: int
    reader_public_id: str
    message_delivery_id: str | None = None
    view_confirmed: bool = False
    created_at: datetime


class PollResponse(BaseModel):
    messages: list[IncomingMessage]
    read_receipts: list[IncomingReadReceipt]
    delivery_receipts: list[IncomingReadReceipt] = Field(default_factory=list)


class MessageReference(BaseModel):
    model_config = {"extra": "forbid"}

    delivery_id: str = Field(..., pattern=r"^[0-9a-f]{32}$")
    chat_id: int = Field(..., gt=0)
    sender_public_id: str = Field(..., min_length=1, max_length=64)
    client_message_id: str = Field(..., min_length=1, max_length=64)


class ReceiptReference(BaseModel):
    model_config = {"extra": "forbid"}

    delivery_id: str = Field(..., pattern=r"^[0-9a-f]{32}$")
    chat_id: int = Field(..., gt=0)
    reader_public_id: str = Field(..., min_length=1, max_length=64)
    client_message_id: str = Field(..., min_length=1, max_length=64)


class ExactAckRequest(BaseModel):
    model_config = {"extra": "forbid"}

    messages: list[MessageReference] = Field(default_factory=list, max_length=100)
    receipts: list[ReceiptReference] = Field(default_factory=list, max_length=100)
    confirm_delivery: bool = False


class ExactMarkReadRequest(BaseModel):
    model_config = {"extra": "forbid"}

    messages: list[MessageReference] = Field(..., min_length=1, max_length=100)
    viewed: bool = False


class MessageStatusRequest(BaseModel):
    model_config = {"extra": "forbid"}

    client_message_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        min_length=1, max_length=100
    )


class RecipientDeliveryStatus(BaseModel):
    public_id: str
    delivery_id: str | None = None
    delivered_at: datetime | None = None
    read_at: datetime | None = None


class MessageDeliveryStatus(TimestampedPayload):
    client_message_id: str
    recipients: list[RecipientDeliveryStatus]


class MessageStatusResponse(BaseModel):
    statuses: list[MessageDeliveryStatus]


# ---------- admin API ----------


class AdminLinkCreate(BaseModel):
    link_type: Literal["personal", "group"]
    note: str | None = Field(default=None, max_length=256)
    expires_in_hours: int | None = Field(default=None, ge=1, le=24 * 365)
    group_title: str | None = Field(default=None, max_length=128)


class AdminLinkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    token: str
    link_type: Literal["personal", "group"]
    url: str
    chat_id: int | None
    max_uses: int
    uses_count: int
    is_active: bool
    revoked_at: datetime | None = None
    note: str | None
    created_at: datetime
    expires_at: datetime | None


class AdminUserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    public_id: str
    display_name: str | None
    key_fingerprint: str
    created_at: datetime
    last_seen_at: datetime | None
    is_active: bool
    chats: list[int]


class AdminChatOut(BaseModel):
    id: int
    chat_type: Literal["personal", "group"]
    title: str | None
    created_at: datetime
    member_public_ids: list[str]
    pending_messages: int


class AdminStats(BaseModel):
    users: int
    links_active: int
    chats: int
    pending_messages: int


class ExportedUser(BaseModel):
    public_id: str
    display_name: str | None
    public_key: str
    created_at: datetime
    is_active: bool


class ExportedChatMember(BaseModel):
    public_id: str
    joined_at: datetime
    resume_hash: str | None = None


class ExportedChat(BaseModel):
    chat_type: Literal["personal", "group"]
    title: str | None
    created_at: datetime
    closed_at: datetime | None = None
    members: list[ExportedChatMember]


class ExportedLink(BaseModel):
    token: str
    link_type: Literal["personal", "group"]
    chat_index: int | None
    max_uses: int
    uses_count: int
    is_active: bool
    password_required: bool = False
    password_hash: str | None = None
    failed_attempts: int = 0
    failed_window_started_at: datetime | None = None
    note: str | None
    created_at: datetime
    expires_at: datetime | None


class ExportBundle(BaseModel):
    version: int = 1
    generated_at: datetime
    users: list[ExportedUser]
    chats: list[ExportedChat]
    links: list[ExportedLink]


__all__ = [
    "_decode_b64",
    "_encode_b64",
    "LinkActivateRequest",
    "LinkActivateResponse",
    "ParticipantInfo",
    "ChatInfo",
    "MeResponse",
    "MessageEnvelope",
    "SendMessageRequest",
    "SendMessageResponse",
    "IncomingMessage",
    "IncomingReadReceipt",
    "PollResponse",
    "AdminLinkCreate",
    "AdminLinkOut",
    "AdminUserOut",
    "AdminChatOut",
    "AdminStats",
    "ExportedUser",
    "ExportedChat",
    "ExportedChatMember",
    "ExportedLink",
    "ExportBundle",
]
