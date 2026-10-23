# Client API

Local `/docs` (admin listener, port 8000) provides the complete schema.
Send `Content-Type: application/json`; JSON routes reject missing/non-JSON types
with 422 and omit supplied values from validation errors. After activation, send
`Authorization: Bearer <JWT>` for authenticated HTTP operations.

## Bundled test web client

`/client` is the unaudited [v1 reference client](PROTOCOL.md), requiring HTTPS or
localhost. `/l/{token}` opens `/client?invite={token}`. Names are unauthenticated
labels; routing uses public IDs. See [device/access lifecycle](SESSION_LIFECYCLE.md)
and [security limits](SECURITY_REVIEW.md#threat-model-and-limits).

## Activate link

```text
POST /api/v1/links/activate
Content-Type: application/json

{
  "token": "<invite admission secret>",
  "public_key": "<base64 uncompressed P-256 public point, 65 bytes>",
  "display_name": "Alice",
  "resume_credential": "<43-character base64url credential>",
  "session_credential": "<separate 43-character base64url credential>"
}
```

`display_name` is optional; protected rooms also require `password`.
`session_credential` is mandatory (missing/null → 422 before admission).
Persist separate random 32-byte resume/session credentials as unpadded base64url
before requesting admission; retries must reuse them.

Activation always requires HTTPS outside direct loopback, even with
`SIDEWORD_REQUIRE_HTTPS=false`. HTTP loopback requires both a loopback peer and
host `localhost`, `127.0.0.1` or `::1`; forwarded headers alone cannot qualify.
Insecure requests return 400 from activation, or 403 from the enabled global TLS guard.

The response contains:

```text
{
  "token": "<client JWT>",
  "session_id": "<registered session ID>",
  "access_expires_at": "<UTC deadline>",
  "session_expires_at": "<UTC absolute deadline>",
  "user": {"public_id": "...", "public_key": "...", "key_fingerprint": "..."},
  "chat": {"id": 1, "chat_type": "personal|group", "participants": [ ... ]}
}
```

The invite secret and returned JWT are different credentials. Invite landing URLs
and activation bodies remain sensitive despite no-store/no-referrer headers:
protect proxy/server logs and browser history. See [deployment controls](UPGRADING.md#public-deployment-controls).

### Admission and retries

- Personal rooms have two slots and seal when full. Groups optionally seal at
  their configured limit, with a maximum of 101 members for 100-recipient fanout.
- A valid supplied bearer reuses its identity after key matching; a new protected
  room still requires its password. Omit Authorization for anonymous/credential-only
  recovery. Supplied malformed, expired or revoked authentication returns 401,
  never anonymous admission. After 401, explicitly remove the header before resume retry.
- Matching invite resume credential/public key recovers the same participant,
  including a lost activation response; a public key alone does not. Only the
  credential's SHA-256 digest is stored. Malformed credentials fail before the DB write reservation.
- SQLite `BEGIN IMMEDIATE` serializes admission. Password guesses are limited to
  five per invite per five-minute window, persisted across restarts; excess returns
  429/Retry-After. IP changes do not reset it. Invite holders can exhaust the
  budget, while authenticated reconnects remain available.
- Expired/revoked/deleted invites cannot resume. Consumed/sealed invites still
  permit authorized reconnects. Membership survives invite invalidation; other
  valid sessions retain identity-wide access. See [lifecycle](SESSION_LIFECYCLE.md).

Passwords are exact, including case/whitespace: generated 16-character/96-bit
secrets or custom 8–32-character phrases. Share separately. Verification uses
scrypt N=2^17, r=8, p=1, random 16-byte salt and 32-byte output. This trusts the
server/TLS terminator, not PAKE. Passwords are never in URLs/plaintext exports;
configuration exports retain verifiers and admission retry metadata.

## Profile and chats

`GET /api/v1/me` returns identity, memberships and participant public keys;
`GET /api/v1/chats/{chat_id}` returns one authorized roster. Each chat has nullable
UTC `closed_at`. Nonmembers receive 404.

Closed chats remain visible to members for saved archives. Sends, new joins,
viewing/read, retry uploads and status queries return
`410 {"detail":"conversation closed"}`; poll/WS/presence omit their queues.
Late ownership-checked durable ACKs and own-outbox deletion remain allowed.
Closure deletes no memberships/local history/relay rows; ordinary TTL still applies.

Admin POST `/admin/chats/{chat_id}/close` or `/reopen` uses normal CSRF protection.
Closure serializes with sends; reopening resumes retained delivery without
restoring invalid credentials. Same-process `chat_state` events notify members;
refresh `/me` for authoritative state, including changes from other workers.

## Send message

```text
POST /api/v1/chats/{chat_id}/messages
{
  "client_message_id": "<unique logical message ID>",
  "envelopes": [{"recipient_public_id": "<peer>", "ciphertext": "<base64>"}]
}
```

Cover exactly every member except the sender, once each. Ciphertext is opaque;
clients must agree on an authenticated [envelope format](PROTOCOL.md).
Default maximum decoded ciphertext is 64 KiB per recipient. Persist envelopes
before upload and use a fresh message ID for each logical message.

## Receive messages and receipts

| Transport | Contract |
| --- | --- |
| `GET /ws`, subprotocol `sideword.v1` | Immediately send first-frame `{"type":"auth","token":"<JWT>"}`. URL/subprotocol credentials are rejected. `hello` includes `user` and `backlog`; live events include `message`, `delivered`, `read`, `chat_state`. |
| `GET /api/v1/poll` | Bearer-authenticated response has `messages`, `delivery_receipts`, `read_receipts`. |

Verify `hello.user` matches the authenticated HTTP identity before using backlog.
Serialize live/backlog/poll processing; events can arrive during initialization.
An initialized socket is not recipient delivery proof. Ignore unknown event types.
`chat_state` carries `chat_id` and `closed_at` (null when reopened).

Aliases must route both transports to one process/database; SQLite sharing alone
cannot broadcast. The test client uses its current origin, with no cross-origin
credential transfer or extra CORS permissions.

## Participant presence

Opt in with first-frame `{"type":"auth","token":"<JWT>","presence":true}`.
After hello, snapshots are:

```json
{
  "type": "presence",
  "valid_for_ms": 35000,
  "chats": [{"chat_id": 1, "participants": ["<A>", "<B>"], "online": ["<A>"]}]
}
```

Snapshots cover current open memberships only. Online means at least one active
authenticated socket, counted once across sessions; a valid session alone is
insufficient. Only a listed member absent from `online` is offline. Missing/expired
snapshots, disconnected clients and unlisted members mean **unknown**.

Replace snapshots; expire on their monotonic deadline (maximum 35 seconds),
disconnect or auth failure. Process immediately, outside delivery queues.
Send textual `ping` at least every 25 seconds; receive `pong` and refreshed
snapshots. Presence sockets close after 75 seconds without an incoming frame;
credential checks are at most 30 seconds apart. Connect/disconnect updates observers.

Presence is unencrypted server connection metadata, not identity/viewing/decryption
proof. It requires one shared process; set `SIDEWORD_PRESENCE_ENABLED=false` on
independent workers/replicas. Sticky routing does not combine registries. Disabled
servers send no snapshots. Preserve transport ping/pong cleanup (Uvicorn defaults 20/20 seconds).

## Exact delivery identities

Each message/receipt has random 32-character lowercase-hex `delivery_id`, stable
for that row and across full backups/restarts. New rows get fresh IDs even if
integer IDs or logical message IDs repeat. This is relay metadata, not a secret
or decryption proof. Owner authorization remains mandatory.

Deduplicate messages by chat/sender/client message ID; receipts also include
reader, original message delivery ID and event stage. Never deduplicate by SQLite
row ID. Treat naive timestamps as UTC.

## Mark read

After durable delivery **and actual viewing**:

```text
POST /api/v1/chats/7/read/exact
{
  "messages": [{"delivery_id":"0123456789abcdef0123456789abcdef",
    "chat_id":7,"sender_public_id":"<sender>","client_message_id":"m-1"}]
}
```

Accepts 1–100 references. Viewing neither deletes ciphertext nor implicitly
confirms delivery; see [confirmation semantics](#durable-delivery-and-actual-viewing).

## Acknowledge server-side deletion

Authenticate/decrypt and commit local history before ACK; failed crypto/storage
must remain retryable.

```text
POST /api/v1/ack/exact
{
  "messages": [],
  "receipts": [{"delivery_id":"fedcba9876543210fedcba9876543210",
    "chat_id":7,"reader_public_id":"<reader>","client_message_id":"m-1"}]
}
```

Messages use exact-read references and belong to the authenticated recipient.
Receipts bind chat/reader/message/delivery to the authenticated original sender.
Each optional list accepts at most 100 items; unknown fields fail validation.
Response: `{"deleted_messages":N,"deleted_receipts":N}`. Retry identical
references after failure; zero can mean the first request committed. Deleted
ciphertext is unavailable from the relay. Stolen bearer credentials can delete
that owner's deliveries without proving decryption.

## Drop own undelivered messages

`DELETE /api/v1/chats/{chat_id}/outbox` deletes only the authenticated sender's
pending ciphertext. It preserves retry evidence.

## Message security model

Public keys must be base64 uncompressed 65-byte P-256 points with valid curve
membership. Invalid keys fail admission, auth, refresh and configuration import.
The server does not inspect envelope contents; compute/verify fingerprints
locally. The [protocol](PROTOCOL.md) owns cryptographic parameters and trust limits.

## Send retries and capacity

Sends deduplicate by chat/authenticated sender/client message ID for
`SEND_IDEMPOTENCY_DAYS` (default 30), measured from acceptance. Identical decoded
ciphertext/recipient sets return the original 200 response; conflicts return 409.
Recipient order/equivalent base64 do not change comparison. Retries after ACK or
partial fanout never recreate rows while the original ledger record exists.

`/me.send_retry_window_seconds` advertises the window. Persist and retry identical
envelopes, never re-encrypt under the same ID. Configuration changes do not extend
old guarantees; beyond the saved deadline verify manually. Full backups preserve
existing evidence; configuration exports do not. Older snapshots lose later send
history and can revive refresh credentials; use [safe restore](UPGRADING.md#restore-safely).

| Default limit | Value |
| --- | --- |
| Request / envelopes per send | 2 MiB / 100 |
| Poll or WS backlog | 100 messages, 100 delivery and 100 read receipts; consume batches until empty |
| Sender queue / bytes / new sends | 1,000 envelopes+receipts / 16 MiB ciphertext / 60 per minute |
| Global queued messages / bytes | 10,000 / 256 MiB |
| HTTP requests | 600 per minute/IP/process |
| WS connections / frames | 256 per process, 4 per user/process; 4 KiB inbound, 120 frames/minute/socket |

Identical retries consume no new capacity. Global receipt/participant/ledger
quotas return 429 without partial fanout or eviction. Complement process-local
limits with proxy controls; see [.env.example](../.env.example).

## Renewable client sessions

Activation issues a registered session from the persisted initial
`session_credential`; JWTs without a live registration fail with 401. The initial
credential can retry lost activation responses before rotation. Issuance rechecks
identity, active user, invite and any supplied session under the write reservation,
including retries. A later validity change returns 401, though admission may
already have committed its slot.

```text
POST /api/v1/sessions/refresh
{"credential":"<current>","next_credential":"<fresh, persisted successor>"}
```

Use HTTPS; no access token is required. Persist the successor before sending and
save returned access token/successor together. Coordinate tabs/processes around
one pending proposal. Only digests are stored. The exact pair may retry for
30 seconds while the successor is current; any other known-consumed reuse revokes
the session. Do not replace a proposal because its response was lost.

Default access lifetime is 15 minutes; absolute session lifetime is 30 days and
never extends on refresh. Invite expiry caps stored issuance lifetime and current
response deadlines. Extending the invite does not extend the stored session.
`/me` exposes deadlines; effective access ends at the earliest JWT/session/invite
expiry. Expiry requires invite recovery; consumed/sealed invites permit refresh,
revoked/deleted/expired invites and inactive users do not.

`GET /api/v1/sessions` lists owned sessions;
`DELETE /api/v1/sessions/{session_id}` revokes one for HTTP/WS. Other sessions and
invite resume credentials survive; see [lifecycle](SESSION_LIFECYCLE.md).

## Admin requests

Fetch an admin page for `csrf_token`; HTML forms submit it, programmatic cookie
clients use `X-CSRF-Token`. It binds the current admin cookie; refresh after login.
Foreign Origins fail. Missing/null Origin on no-referrer forms requires valid CSRF
and `Sec-Fetch-Site: same-origin`; clients without Origin/Fetch Metadata need the
CSRF header. Cookie-free admin bearer requests are CSRF-exempt but still need a
registered admin session. Logout/password resets revoke sessions.

Login rechecks the verified password inside a write transaction: concurrent resets
return 401. Expired admin sessions are pruned before enforcing the 1,000-session
cap; capacity returns 429 without live-session eviction. Admin/schema routes are
local-only by default; use HTTPS outside loopback.

## Durable delivery and actual viewing

Acceptance, durable delivery and viewing are distinct. Send responses include
original `recipients` and a `deliveries` public-ID→delivery-ID map. Preserve them;
later membership changes must not alter original confirmation requirements.

Exact ACK deletes ciphertext, records delivery once and queues a sender
`delivery_receipts` event. Poll/hello include that array; live events use
`{"type":"delivered","delivery":{...}}`. Receipt payloads include the original
`message_delivery_id`; their own `delivery_id` identifies receipt ACKs. Streaming
or returning ciphertext alone never confirms durable delivery.

Exact read matches the retained ledger's chat/sender/recipient/message/delivery,
requires prior delivery and current membership, and records the first viewing
time once. Response: `{"marked":N}`. Duplicate/stale/unauthorized recipient or
out-of-order references mark zero; nonmembers receive 404. Read events carry
`view_confirmed:true` and original `message_delivery_id`. These are authenticated
recipient assertions, not proof of a person reading; never infer from presence.

The browser requires positive integer `/me.receipt_retention_seconds` before
viewing/status recovery. It confirms viewing after 750 ms with at least half a
small message (or 48 vertical pixels of a tall one) and half its width visible,
in the active chat, focused/visible document and no dialog. Loss of visibility
resets dwell; missing observation support fails closed. Persist viewing intent
for offline retries. Durable ACKs proceed independently.

## Message status recovery

```text
POST /api/v1/chats/7/messages/status
{"client_message_ids":["m-1"]}
```

Requires membership; returns only the authenticated sender's retained records.
Accepts 1–100 IDs, 1–64 characters each, with no unknown fields. Each `statuses`
entry has `client_message_id`, `created_at` and original `recipients` with
`public_id`, `delivery_id`, `delivered_at`, `read_at` (unconfirmed times null).
Unknown/expired/other senders' IDs are omitted. No content, names, keys, sessions
or device activity is returned.

Ledger metadata lasts until the later original retry deadline and message TTL
from acceptance. `/me.receipt_retention_seconds` advertises the maximum current
horizon without extending old retry expiry. Cleanup is startup/hourly. Full
backups preserve it; config exports do not. Receipt queues retain ordinary TTL
and capacity. At capacity, ACK/viewing still commits and status recovery supplies
missing events; neither receipt stream starves the other's 100-item batch.

## Receipt history

Track acceptance/delivery/viewing independently for every original recipient.
Aggregate completion requires all original recipients; missing confirmation means
pending, not failed. Recover until all confirm viewing or metadata expires.
Keep UTC timestamps/order.

Encrypt immutable acceptance, exact references, receipts, viewing intent and ACK
markers with the history key/AAD. Hash new event keys to avoid exposing recipient
bindings in plaintext storage keys. Deduplicate receipts by chat, reader, client
message ID, original delivery ID and stage; ACK each receipt's own reference only
after persistence. Merge WS/poll/other-tab data without overwriting independent
recipients. Receipts are history metadata, never separate timeline messages.
