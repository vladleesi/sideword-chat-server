from datetime import datetime, timezone

from app.schemas import IncomingMessage, IncomingReadReceipt, SendMessageResponse


def test_sqlite_naive_timestamps_are_serialized_as_utc():
    fields = dict(client_message_id="uuid", created_at=datetime(2026, 9, 26, 2))
    payloads = [
        IncomingMessage(**fields, delivery_id="a" * 32, id=1, chat_id=1,
                        sender_public_id="a", ciphertext="eA=="),
        IncomingReadReceipt(**fields, delivery_id="a" * 32, id=1, chat_id=1,
                            reader_public_id="b", message_delivery_id="b" * 32,
                            view_confirmed=True),
        SendMessageResponse(**fields, recipients=["b"], content_expires_at=fields["created_at"]),
    ]
    for payload in payloads:
        assert payload.created_at.tzinfo == timezone.utc
        assert payload.model_dump(mode="json")["created_at"] == "2026-09-26T02:00:00Z"
