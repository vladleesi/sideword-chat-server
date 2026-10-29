# Client API

Local `/docs` on admin port 8000 provides complete request/response schemas.
JSON routes require `Content-Type: application/json` (otherwise 422).
Authenticated HTTP requests use `Authorization: Bearer <JWT>`. Use HTTPS outside
loopback; activation enforces this even when the global HTTPS guard is disabled.

## Activate link

`POST /api/v1/links/activate`:

```json
{
  "token": "<invite secret>",
  "public_key": "<base64 uncompressed P-256 point, 65 bytes>",
  "display_name": "Alice",
  "resume_credential": "<43-character base64url credential>",
  "session_credential": "<separate 43-character base64url credential>"
}
```

Persist separate random 32-byte resume/session credentials as unpadded base64url
before admission; reuse them on retries. `session_credential` is mandatory.
Protected rooms also require `password`. Optional `display_name` is trimmed,
blank becomes null, maximum 64 characters; it belongs to this membership and
survives reconnects. Response includes JWT, session ID/deadlines, user and chat.

Personal rooms have two slots; groups allow at most 101 members. A valid supplied
bearer/key reuses its identity; saved invite resume credential/key can recover it
without a bearer. Invalid supplied authentication returns 401, never anonymous
admission. Explicitly remove a rejected bearer before credential-only recovery.
Consumed/sealed invites allow reconnects; revoked/expired/deleted invites do not.
See [access rules](SESSION_LIFECYCLE.md).

Passwords are exact, case/whitespace-sensitive: generated 16-character secrets or
custom 8–32-character phrases. Five failed guesses per invite/five minutes persist
across restarts; excess returns 429/Retry-After. Share passwords separately and keep
invite URLs, credentials and request bodies out of logs.

## Profile and chats

- `GET /api/v1/me`: identity, all memberships, participant keys, retry/receipt windows
  and session deadlines. Use `chat.participants[].display_name` for room names;
  top-level `user.display_name` is the initial account label.
- `GET /api/v1/chats/{chat_id}`: authorized roster and nullable UTC `closed_at`.
  Nonmembers receive 404.
- Closed chats return 410 for sends, viewing/status and retry uploads; poll/WS/presence
  omit their queues. Late exact ACKs and own-outbox deletion remain allowed.

## Send message

`POST /api/v1/chats/{chat_id}/messages`:

```json
{"client_message_id":"m-1","envelopes":[{"recipient_public_id":"<peer>","ciphertext":"<base64>"}]}
```

Cover every member except the sender exactly once. Persist immutable envelopes
before upload. Each logical message needs a unique ID and an agreed [protocol](PROTOCOL.md).
File metadata/bytes stay inside ciphertext; there is no attachment URL/endpoint.
Acceptance includes original `recipients`, a public-ID→`delivery_id` map, `created_at`
and fixed `content_expires_at` (seven days by default, configurable 1–30).

## Receive messages and receipts

| Transport | Contract |
| --- | --- |
| `GET /ws`, subprotocol `sideword.v1` | First frame: `{"type":"auth","token":"<JWT>"}`. Credentials in URLs/subprotocols are rejected. `hello` contains `user`/`backlog`; live events: `message`, `delivered`, `read`, `chat_state`. |
| `GET /api/v1/poll` | Bearer-authenticated; returns `messages`, `delivery_receipts`, `read_receipts`. |

Verify `hello.user` against the HTTP identity; serialize WS/backlog/poll processing
and ignore unknown event types. `chat_state` supplies `chat_id`/`closed_at`.
Aliases must route HTTP/WS to one process/database; no cross-origin credential transfer.

## Participant presence

Opt in with `"presence":true` in WS auth. Snapshots contain `type:"presence"`,
`valid_for_ms` and `chats:[{chat_id,participants,online}]`. Replace snapshots and expire
on their monotonic deadline (at most 35 seconds), disconnect or auth failure.
Only a listed participant absent from `online` is offline; otherwise status is unknown.
Send textual `ping` at least every 25 seconds; expect `pong`/fresh snapshots.
Presence sockets close after 75 seconds without incoming frames; auth checks occur
at most 30 seconds apart. Presence is connection metadata, not viewing/decryption proof.
Disable `SIDEWORD_PRESENCE_ENABLED` across independent workers; preserve transport ping/pong.

## Exact delivery identities

Messages/receipts have random 32-character lowercase-hex `delivery_id`s. SQLite row
IDs can be reused: deduplicate messages by chat/sender/client message ID, receipts
also by reader/original delivery ID/stage. Normalize naive timestamps to UTC.

Message reference: `{delivery_id,chat_id,sender_public_id,client_message_id}`.
Receipt reference: `{delivery_id,chat_id,reader_public_id,client_message_id}`.
Both bind the exact owned delivery; IDs are relay metadata, not decryption proof.

## Acknowledge server-side deletion

After authenticating, decrypting, validating and committing encrypted local history:

```text
POST /api/v1/ack/exact
{"messages":[<message reference>],"receipts":[<receipt reference>]}
```

Each optional list accepts at most 100 references; unknown fields fail validation.
Response: `{"deleted_messages":N,"deleted_receipts":N}`. Retry identical references;
zero may mean an earlier request committed. Crypto/storage failure must withhold ACK.
ACK atomically deletes that recipient's ciphertext and records durable delivery.
Stolen bearer credentials can delete their owner's queue without proving decryption.

`DELETE /api/v1/chats/{chat_id}/outbox` removes only the sender's pending ciphertext;
retry evidence remains. Expired copies stop delivery at their fixed deadline;
startup/hourly cleanup deletes them. See [retention limits](SECURITY_REVIEW.md#delivery-local-storage-and-retention).

## Durable delivery and actual viewing

Acceptance, durable delivery and viewing are separate. ACK produces `delivery_receipts`;
live form: `{"type":"delivered","delivery":{...}}`. Receipt `message_delivery_id`
refers to the original message; its own `delivery_id` is used for receipt ACKs.

After durable delivery **and actual viewing**, POST `/api/v1/chats/{chat_id}/read/exact`
with `{"messages":[<message reference>]}` (1–100). Response: `{"marked":N}`.
Matching prior delivery/current membership is required; duplicates, stale or out-of-order
references mark zero. Read events include `view_confirmed:true` and original delivery ID.

The client confirms viewing after 750 ms of sufficient visibility in a focused active
chat without a dialog; loss of visibility resets dwell. Persist viewing intent for
retries; missing visibility support fails closed. Attachment viewing means card visibility,
not opening/downloading its file. Downloads never gate durable ACKs or send read events.

## Send retries and capacity

Deduplication binds chat/sender/client message ID for the fixed acceptance retry window
(default 30 days, advertised as `/me.send_retry_window_seconds`). Identical decoded
ciphertext/recipients return the original 200 response; conflicts return 409. Recipient
order/equivalent base64 do not matter. Retries never recreate ACKed/expired copies
while that ledger exists. Never re-encrypt under the same ID; verify before retrying
beyond the saved window or after restoring an older backup.

| Default limit | Value |
| --- | --- |
| Decoded envelope / send body / other HTTP body | 2 MiB / 10 MiB / 2 MiB |
| Recipients per send | 100 |
| Poll/WS backlog | 100 messages within 3 MiB wire budget, plus 100 receipts of each kind |
| Sender queue / bytes / new sends | 1,000 envelopes+receipts / 16 MiB / 60 per minute |
| Global queue / bytes | 10,000 / 256 MiB |
| Per-chat queue / bytes | 2,000 / 32 MiB |
| HTTP | 600/minute/IP/process; 4 concurrent operations, plus 32 static GET/HEAD slots |
| WS | 256/process, 4/user/process; 4 KiB inbound, 120 frames/minute/socket |

Bodies are bounded cumulatively regardless of Content-Length, with a 15-second deadline.
Oversize returns 413/422, excess fanout 422, capacity 429 without partial fanout/eviction.
Outgoing WS frames are bounded to 4 MiB/five seconds; busy live pushes are skipped and
recovered by polling. Client WS buffering is capped at 6 MiB/32 events.

Server limits bound ciphertext/resources, not verified file size/type. Client limits:
1 MiB decoded files and 16,000 UTF-8 text bytes. Four maximal file copies fit a send;
larger groups need smaller files. See [encoding budget](PROTOCOL.md#encoding-and-resource-budget)
and [.env.example](../.env.example) for other quotas/configuration.

## Renewable client sessions

`POST /api/v1/sessions/refresh` with
`{"credential":"<current>","next_credential":"<persisted fresh successor>"}`.
Requires HTTPS, no bearer. Persist the successor before sending; coordinate tabs around
one pending proposal and save the returned JWT/successor together. Only digests are stored.
The exact pair can retry for 30 seconds while the successor is current; other known-consumed
reuse revokes the session. Never replace a proposal after losing its response.

Access defaults to 15 minutes; absolute session lifetime to 30 days, capped by invite
expiry and never extended by refresh. JWTs require live session registration.
`GET /api/v1/sessions` lists owned sessions; `DELETE /api/v1/sessions/{session_id}`
revokes one across HTTP/WS. Invite resume credentials survive session revocation.

## Message status recovery

POST `/api/v1/chats/{chat_id}/messages/status` with `{"client_message_ids":["m-1"]}`
(1–100 IDs, each 1–64 characters). Returns only the sender's retained records;
unknown/expired/other senders' IDs are omitted. Each status includes acceptance/content
deadlines and original recipients with `public_id`, `delivery_id`, nullable
`delivered_at`/`read_at`, and state `pending|delivered|expired|unavailable`.
Expiry/unavailability means no confirmed delivery or deliverable copy, not proof that
nothing was saved. Late confirmations win; the client never silently resends.

Metadata lasts until the later fixed retry/content deadline. `/me.receipt_retention_seconds`
advertises the current horizon without extending old guarantees. At receipt capacity,
ACK/viewing still commits; status recovery supplies missing events. Track each original
recipient independently and encrypt acceptance, receipts, viewing intent and ACK markers;
ACK receipt events only after persistence. See [protocol delivery rules](PROTOCOL.md#delivery-requirements-for-future-clients).

## Admin requests

Cookie requests need a current `csrf_token` form field or `X-CSRF-Token` header.
Foreign Origins fail; missing Origin on forms requires CSRF plus `Sec-Fetch-Site:
same-origin`. Clients without Origin/Fetch Metadata need the header. Cookie-free bearer
requests are CSRF-exempt but still need a registered admin session. Logout/password
resets revoke sessions. Admin/schema routes are local-only by default.
