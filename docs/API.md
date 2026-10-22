# Client API

See `/docs` on the local administration listener (port 8000) for the complete
schema. Send JSON bodies with `Content-Type: application/json`. After activation,
HTTP API requests require `Authorization: Bearer <JWT>`.
JSON endpoints reject missing or non-JSON content types with 422; validation
responses omit submitted values.

For password-protected rooms, include `password` in activation. Persist a random
`resume_credential` and a separate `session_credential` before the first activation
request so retries reuse the same participant slot/session. See [activation rules](#activate-link) for room limits,
passwords and access expiry, and [invites and chats](../README.md#invites-and-chats)
for browser use.

## Bundled test web client

`/client` is a reference client for the unaudited [v1 wire format](PROTOCOL.md),
using HTTPS or localhost. `/l/{token}` links to `/client?invite={token}`.
See [invites and chats](../README.md#invites-and-chats) for usage and reset warnings,
and [SECURITY_REVIEW.md](SECURITY_REVIEW.md) for trust boundaries. Display names
are unauthenticated labels; message and receipt identities use public IDs.

## Activate link

Send the invite admission secret as `token` in the JSON body of
`POST /api/v1/links/activate`, keeping it out of the activation URL. It is separate
from the client JWT returned by activation. The bundled client uses this route.
Treat request bodies and complete invite landing URLs as sensitive: HTTPS
encrypts them in transit, but servers, TLS terminators, proxies or monitoring
tools can still record them. Anyone who obtains an unprotected, open invite can
claim a participant slot. Suppress or redact invite URLs and bodies at every
logging layer; see [public deployment controls](UPGRADING.md#public-deployment-controls).
Responses use `Cache-Control: no-store` and `Referrer-Policy: no-referrer`, which
do not erase logs or browser history from opening invite links.

Activation always requires HTTPS outside local loopback, even when
`SIDEWORD_REQUIRE_HTTPS=false`. The HTTP loopback exception requires both a
loopback peer address and a `localhost`, `127.0.0.1`, or `::1` host. An insecure
request is rejected before admission (400 from the endpoint when the global TLS
guard is disabled; otherwise 403 from that guard). Arbitrary forwarded headers
do not qualify.

```
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

Send the invite secret in the JSON body. Invite landing paths `/l/{token}` and
`/client?invite={token}` also contain secrets and require URL log protection.

Room passwords are optional. The admin can generate a 16-character password or
set a custom phrase of 8–32 characters, matched exactly including case and whitespace.
Share passwords separately from invite links.

`display_name` is optional; include `password` for protected rooms. The response
contains a client JWT, user identity, and chat participants. `session_credential`
is required for [renewable sessions](#renewable-client-sessions), including the
session ID and deadline fields described there. Missing/null credentials return
422 before admission; JWTs without a registered session ID return 401.

```text
{
  "token": "<client JWT>",
  "session_id": "<registered session ID>",
  "access_expires_at": "<UTC access deadline>",
  "session_expires_at": "<UTC absolute session deadline>",
  "user": { "public_id": "...", "public_key": "...", "key_fingerprint": "..." },
  "chat": {
    "id": 1,
    "chat_type": "personal|group",
    "participants": [ { "public_id": "...", "public_key": "...", ... } ]
  }
}
```

- Personal links allow **two participants**; once full, they seal against new joins.
  Authenticated reconnects reuse the existing slot.
- Group links share a chat and optionally seal at their participant limit.
  Admission is also capped at 101 members to fit the 100-recipient send limit.
- A valid existing bearer session reuses its current user when joining another chat.
  Sessions authenticate that identity across all its memberships. Revoking or
  expiring one issuing invite does not remove those memberships; another valid
  session can still list, read and send in open chats. Explicit conversation
  closure blocks live messaging for every session while retaining saved history.
  An unrelated invite or public key alone cannot recover them. See
  [lifecycle rules](SESSION_LIFECYCLE.md)
  for local history, participant removal and conversation termination.

Omit `Authorization` for a new anonymous join or credential-only recovery. If
supplied, it must contain a valid, live client bearer session; malformed, expired,
or revoked authentication returns 401 without consuming a slot. To recover with
a saved resume credential after 401, explicitly remove the invalid header and
retry with the same public key and resume credential. The bundled client clears
the rejected access token and keeps device keys and saved admission credentials
for that retry. A valid session still requires the password when joining a new
protected room.

### Admission and retries

Generate a separate cryptographically random 32-byte `resume_credential` for
each invite, encode it as unpadded base64url (43 characters), and persist it
with the device identity before activation. A retry with the same credential
and public key reuses the participant even if the original response was lost;
the server stores only the credential's SHA-256 digest. A valid bearer JWT can
also reconnect without another password or slot, but a public key alone cannot.
Malformed resume/session credentials return 422 before the database write
reservation is acquired.

Admission uses a SQLite `BEGIN IMMEDIATE` transaction to prevent concurrent
joins from overfilling rooms. Failed password guesses are limited to five per
invite per five-minute fixed window, persisted in SQLite. Further attempts
return 429 with `Retry-After`; changing IP addresses does not reset the limit.
Anyone holding an invite can exhaust its guess budget, while authenticated
reconnects remain available. Access through an expired or revoked issuing invite
cannot be restored with a password or resume credential. Other valid sessions
retain identity-wide membership access as described above.

Password verification uses OpenSSL-backed scrypt with N=2^17, r=8, p=1, a random
16-byte salt, and a 32-byte result. Generated passwords contain 96 random bits.
This is server verification over HTTPS, not PAKE: the server and TLS terminator
must be trusted. Passwords are never included in URLs or stored or exported in
plaintext. Admin exports preserve verifiers and admission retry metadata.

## Profile and chats

```
GET /api/v1/me
Authorization: Bearer <JWT>
```

Returns the current user, chats, and each participant’s public key (used to encrypt outbound envelopes).

Each chat includes nullable `closed_at` (a UTC timestamp). Closed conversations
remain in this roster and `GET /api/v1/chats/{chat_id}` for authenticated members
so clients can read their saved history. They reject sends, new admissions,
viewing/read requests, retry uploads and message-status queries with
`410 {"detail":"conversation closed"}`. Their pending ciphertext and queued
receipts are excluded from polling, WebSocket backlog/live delivery and presence.
Late ownership-checked durable ACKs remain allowed for messages persisted before
closure; members may also explicitly delete their own queued outbox ciphertext.
Closure never deletes memberships, local history, outboxes or relay rows;
normal server retention still applies.

Authenticated administration can POST `/admin/chats/{chat_id}/close` or `/reopen`
with the usual CSRF protections. Closure is serialized with sends across workers.
Reopening resumes authorized delivery of retained queues, subject to their TTL,
and leaves invite revocation/expiry and session validity unchanged. The server
enforces closure independently of client state.

## Send message

```
POST /api/v1/chats/{chat_id}/messages
Authorization: Bearer <JWT>

{
  "client_message_id": "uuid or hash",
  "envelopes": [
    { "recipient_public_id": "<peer>", "ciphertext": "<base64>" },
    ...
  ]
}
```

Rules:

- `envelopes` must cover **exactly all** chat members except the sender.
- `ciphertext` is opaque base64 data. All participants must agree on an
  authenticated encryption format; see the security model below.
- Max ciphertext length: `SIDEWORD_MAX_CIPHERTEXT_BYTES` (default 64 KiB).
- `client_message_id` ties to local history and receipts.

## Receive messages and receipts

1. **WebSocket** (recommended):

   ```
   GET /ws
   Sec-WebSocket-Protocol: sideword.v1
   ```

   Immediately send `{"type":"auth","token":"<JWT>"}` as the first frame.
   The `hello` frame includes backlog; `message`, `delivered` and `read` events follow live.
   Same-process admin closure/reopening sends
   `{"type":"chat_state","chat_id":7,"closed_at":"<UTC timestamp>"}`
   only to current members (`closed_at` is null on reopening).
   Refresh `/me` to obtain authoritative state; ignore
   unknown event types for forward compatibility. Periodic `/me` refresh also
   observes changes from other workers. Session authentication and closure
   authorization remain server-enforced even if a client misses the event.
   Credentials in query strings or WebSocket subprotocols are rejected.

   Check that `hello.user` matches the authenticated HTTP participant before
   consuming its backlog. The bundled client marks the connection live after
   roster/backlog processing; this is not a recipient delivery confirmation.
   Live events may race initialization, so serialize incoming processing and
   preserve retries instead of dropping events received before `hello`.

2. **Polling fallback**:

   ```
   GET /api/v1/poll
   Authorization: Bearer <JWT>
   ```

   Returns `messages`, `read_receipts` and `delivery_receipts` arrays.

Room routing uses authenticated membership, not the access hostname. Aliases must
route both HTTP and WebSocket traffic to the same process and database for shared
live delivery; a shared database alone is insufficient. The bundled client uses
its current origin for both transports, without cross-origin credential sharing
or additional CORS permissions. See [device/origin behavior](SESSION_LIFECYCLE.md#persistence-devices-and-reconnects).

## Participant presence

Closed conversations are excluded from presence snapshots.
Presence is optional and uses the existing `/ws` connection. Send
`{"type":"auth","token":"<JWT>","presence":true}` as the first frame to opt in.
After `hello`, the server sends complete `presence` snapshots:

```json
{
  "type": "presence",
  "valid_for_ms": 35000,
  "chats": [{
    "chat_id": 1,
    "participants": ["<public ID A>", "<public ID B>"],
    "online": ["<public ID A>"]
  }]
}
```

Snapshots include only the authenticated user's current conversation memberships.
`online` contains participants with at least one authenticated active socket,
including this device; session validity alone never makes a participant online.
Multiple sockets/sessions count once. An offline participant is a listed member
absent from `online`; missing snapshots, unlisted members and lost connectivity
mean **unknown**, never offline. Clients must replace previous snapshots and
discard them at their monotonic `valid_for_ms` deadline (at most 35 seconds), on
disconnect, or on authentication failure. Process them immediately rather than
behind delivery queues. Presence is server-reported connection status, not proof
of identity, attention, or successful message decryption.

Opted-in clients send textual `ping` at least every 25 seconds; the server replies
`pong` and refreshes their snapshot. These sockets close after 75 seconds without
an incoming frame, with periodic credential checks at most 30 seconds apart.
Connect/disconnect transitions update observers immediately. Presence is optional;
sockets without presence use transport ping/pong cleanup. Keep Uvicorn's ping
interval/timeout enabled (defaults: 20/20 seconds).

Presence is process-local. Use one shared process (including the bundled
two-listener runner); set `SIDEWORD_PRESENCE_ENABLED=false` on every independent
worker/replica in a multi-process deployment. Disabled servers send no presence
snapshots, and clients show unknown. Sticky routing does not make separate
registries authoritative. No broker or additional dependency is required.

## Exact delivery identities

Every message and receipt returned by polling, WS backlog, or live WS events has
a `delivery_id`: a server-generated, random 32-character lowercase hex
identifier. It stays stable while that row exists, including database restarts
and backups. A newly queued row gets a fresh identity even if SQLite reuses its
integer `id` or a sender repeats its `client_message_id`. It is metadata, not a
secret or proof of decryption. All operations still require the owner's JWT.

Use the exact endpoints below. The bundled browser retains failed acknowledgements
for retry.

## Mark read

After durable acknowledgement and actual viewing, post 1–100 exact message
references to `POST /api/v1/chats/{chat_id}/read/exact`:

```json
{
  "messages": [{
    "delivery_id": "0123456789abcdef0123456789abcdef",
    "chat_id": 7,
    "sender_public_id": "sender-public-id",
    "client_message_id": "m-1"
  }]
}
```

See [delivery and viewing](#durable-delivery-and-actual-viewing) for authorization
and retry guarantees. Viewing never deletes ciphertext or confirms delivery
implicitly.

## Acknowledge server-side deletion

Authenticate, decrypt, and persist messages locally before acknowledging them.
Keep failures retryable and serialize incoming processing.

```
POST /api/v1/ack/exact
{
  "messages": [],
  "receipts": [{
    "delivery_id": "fedcba9876543210fedcba9876543210",
    "chat_id": 7,
    "reader_public_id": "reader-public-id",
    "client_message_id": "m-1"
  }]
}
```

`messages` accepts the same references as exact read and always confirms durable
delivery when it removes ciphertext. `receipts` binds the delivery, chat, reader, and client message
ID to the authenticated original sender. Both lists are optional and limited to
100 items each; unknown fields are rejected. The response is
`{"deleted_messages": N, "deleted_receipts": N}`. Repeat a failed request with
the same references; a successful retry can report zero if the first committed.

After ACK, rows are gone from the server. SQLite row IDs may be reused: deduplicate
messages by chat, sender, and client message ID; receipts additionally include
the original message delivery ID and confirmation stage. Treat timestamps without a timezone as UTC when ordering history.

Sends deduplicate within the retry window described below. Use a fresh client
message ID for each logical message. Stolen bearer tokens can still delete their
owner's deliveries through the exact API.

## Drop own undelivered messages

```
DELETE /api/v1/chats/{chat_id}/outbox
```

Deletes pending ciphertext your user sent that recipients have not durably acknowledged yet.

## Message security model

The server contract requires a valid uncompressed 65-byte base64 P-256 public point
and validates curve membership using the `cryptography` library. It treats each
ciphertext envelope as opaque base64 data. Production clients must agree on an
authenticated envelope format, such as the bundled [v1 contract](PROTOCOL.md).
The server's `key_fingerprint` is a convenience, not a trust anchor: calculate and
verify fingerprints locally. V1 has no recipient forward secrecy or recovery
after key compromise. See [threat model and limits](SECURITY_REVIEW.md#threat-model-and-limits)
for relay, browser, bearer-token and retention boundaries.

Invalid public keys fail authentication, renewal, admission and configuration
import. The bundled client validates the [v1 envelope format](PROTOCOL.md);
the server does not inspect its contents.

## Send retries and capacity

`POST /api/v1/chats/{chat_id}/messages` is idempotent by chat, authenticated sender, and
`client_message_id` for `SEND_IDEMPOTENCY_DAYS` (default 30). Identical decoded
ciphertext and recipient sets return the original 200 response; conflicting
payloads return 409. An identical retry after read/ACK or partial fanout delivery
does not recreate deleted rows. Recipient order and equivalent base64 encoding
do not change the comparison. IDs must be unique for each logical message.
The guarantee requires the ledger record from the first successful upload.

`/me.send_retry_window_seconds` advertises the current window. Persist envelopes
before uploading; never re-encrypt a retry under the same ID. A shorter later
configuration must not be assumed to extend old records. Beyond the saved window,
verify delivery manually. The browser retains expired retries for review/discard.
Server outbox deletion also preserves retry evidence. A complete database backup
preserves the ledger; configuration exports do not. Restoring an older backup can
lose evidence of later sends and replay later refresh credentials.

Requests are limited to 2 MiB by default, envelopes to 100 per send, and poll/WS
backlog to 100 messages plus 100 read and 100 delivery receipts. Poll and consume batches until empty.
A sender may have 1,000 outstanding envelopes/receipts and 16 MiB queued
ciphertext, with at most 60 new sends/minute; identical retries consume no new
capacity. Global queue, byte, receipt, participant and ledger quotas return 429 without
partial fanout or eviction. Default request rate is 600/minute/IP/process;
WS supports 256 connections/process, 4/user/process, 4 KiB inbound frames and
120 inbound frames/minute/socket. Proxy limits must complement these counters.

## Renewable client sessions

On activation, include the required `session_credential`: 32 random bytes encoded
as 43 unpadded base64url characters, persisted before the request. The response
includes `session_id`, `access_expires_at`, and `session_expires_at`. Identical
initial credentials can retry a lost activation response before their first rotation.
All client access JWTs require a live registered session issued through activation.
Issuance rechecks the current participant identity, active state, invite, and any
supplied authenticated session under the database write reservation, including
retries of an existing credential.
A validity change after admission/authentication returns 401 without issuing a
new session; admission may already have committed the participant slot.

POST `/api/v1/sessions/refresh` without an access token, over HTTPS, with
`{"credential":"<current-secret>","next_credential":"<fresh-persisted-secret>"}`.
Persist the proposed successor before sending. Save the returned token and promote
the successor together; coordinate tabs/processes so they propose the same retry.
The server stores only digests. The exact pair can retry for 30 seconds while
its successor is current. Any other reuse of a known consumed credential revokes
the session. Never replace a pending proposal merely because a response was lost.
Access defaults to 15 minutes; the absolute session lifetime defaults to 30 days
and never extends on refresh. Refresh after that deadline requires invite recovery.
New session lifetimes are capped by the invite expiry at issuance. Activation
and refresh responses cap both deadline fields by the current invite
expiry. Extending an invite does not extend a session's stored lifetime.

GET `/api/v1/sessions` lists the authenticated user's sessions; DELETE
`/api/v1/sessions/{session_id}` revokes one. Revocation affects HTTP and WS session
checks. `/me` reports `access_expires_at` and `session_expires_at`; access ends at
the earliest JWT, session or invite deadline. Consumed/sealed invites still permit valid session refresh;
revoked, deleted, expired invites and inactive users do not.

## Admin requests

Fetch an admin page before submitting a form. HTML forms include `csrf_token`;
programmatic cookie clients send the rendered token as `X-CSRF-Token`. Tokens are
bound to the current admin cookie. Refresh the token after login. Cross-origin
unsafe requests are rejected. A missing or null Origin from a `no-referrer` form
is accepted only with browser Fetch Metadata `Sec-Fetch-Site: same-origin` and
a valid CSRF token. Clients lacking Origin and Fetch Metadata require the CSRF
header. Explicit foreign origins are always rejected.
Cookie-free admin API bearer clients are exempt from CSRF, but their JWT must
reference a live admin session. Logout and CLI password resets revoke sessions.
Session creation rechecks the verified password record inside a SQLite write
transaction, so a concurrent reset cannot be bypassed by an in-flight login.
Logins return 401 if that record changed. The global 1,000-session cap is enforced
in the same transaction; expired admin sessions are removed before checking it.
At capacity, login returns 429 without evicting any live session.
Admin JWTs require a registered active session. Administrator/schema endpoints
are local-only by default; HTTPS is required outside loopback for all APIs.

## Durable delivery and actual viewing

Server acceptance, durable delivery and viewing are distinct. The send response
includes the original `recipients` and a `deliveries` map from public ID
to exact message delivery ID. Preserve this set; subsequent membership changes
must not change an original message's aggregate confirmation requirements.

After authenticating, decrypting and committing local storage, post the message's
exact reference to `/api/v1/ack/exact`. This deletes
ciphertext independently of viewing, records durable delivery once, and queues a
`delivery_receipts` event for the sender. Poll and WS `hello.backlog` include this
array; live WS events have `{"type":"delivered","delivery":{...}}`.
Delivery events use the existing receipt reference/acknowledgement format and add
`message_delivery_id` for the original ciphertext row. Streaming to a socket or
poll response alone does not confirm durable delivery.

After actual viewing, use [exact read](#mark-read) with the same message reference.
The server requires prior confirmed delivery and matches the
original chat, sender public ID, recipient identity, client message ID and random
delivery ID in the retained ledger. The recipient must still belong to the chat.
It records the first viewing timestamp once, without retaining ciphertext, and
returns `{"marked": N}`. Duplicate, stale, unauthorized recipient references and
out-of-order viewing before delivery mark zero; nonmembers receive 404 to conceal
conversation existence. Retries
use the original reference. Read events add `"view_confirmed": true` and
`message_delivery_id`; their own `delivery_id` remains the receipt ACK identity.
These are authenticated recipient assertions, not cryptographic proof of a person
reading. Never infer confirmation from presence.

The browser confirms viewing only after at least half of a small message (or
48 vertical pixels of a tall message) and half its width remain visible in the
message viewport for 750 ms, in the active conversation, with a visible, focused
document and no open client dialog. Hiding, leaving or losing focus resets the
dwell. Without intersection observation it fails closed. Durable ACKs continue
independently. Persist viewing intent before sending so offline failures retry.

The current protocol always separates durable delivery from actual viewing.
The client requires a positive integer `receipt_retention_seconds` from its
authenticated profile before viewing/status recovery. Unsupported response formats
fail closed.

## Message status recovery

```
POST /api/v1/chats/7/messages/status
{"client_message_ids":["m-1"]}
```

Requires conversation membership and returns only the authenticated sender's
records. Accepts 1–100 IDs, each 1–64 characters; unknown fields are rejected.
Response `statuses` entries contain `client_message_id`, `created_at` and the
original `recipients` array with each recipient's `public_id`, `delivery_id`,
`delivered_at` and `read_at`. Unconfirmed times are null until confirmation.
Unknown, expired and other senders' IDs are omitted. No message
content, names, keys, sessions or device activity is returned.

The bounded send ledger retains this metadata until the later of its original
retry expiry and message TTL measured from acceptance. `/me.receipt_retention_seconds`
advertises the current maximum horizon; it does not extend existing retry expiry.
Cleanup runs at startup/hourly. Full backups preserve metadata; configuration
exports do not. Receipt queues keep their message TTL and existing capacity limit.
If receipt capacity is exhausted, durable ACK/viewing state still commits and
sender-only status recovery supplies the missing event. Ciphertext never waits
for human viewing. Read and delivery queues each return at most 100 events per
poll/backlog, so neither event stream starves the other.

## Receipt history

Track server acceptance, delivery and viewing separately for each original
recipient. Complete aggregate delivery requires confirmation from all original
recipients; viewing by one recipient does not imply viewing by the rest. Recovery
continues until every original recipient confirms viewing or metadata expires.
Missing confirmation is pending, not proof that delivery failed. Keep stored
timestamps and ordering in UTC; local display time does not change their meaning.

Store immutable acceptance, receipt, exact reference, viewing intent and ACK
records encrypted with the existing history key/AAD; hash new event keys to keep
recipient bindings out of plaintext database keys. Deduplicate receipts by
chat, reader public ID, client message ID, original message delivery ID and event
stage; ACK each received receipt's own exact reference only after persistence.
Merge polling, WS and other-tab records without overwriting independent recipients.
Status recovery covers events consumed in other tabs or omitted under queue limits.
Receipt events are encrypted local metadata, never standalone timeline messages.
They carry explicit reader, message and delivery identities.
