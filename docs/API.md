# Client API

See `/docs` on the local administration listener (port 8000) for the complete
schema. Send JSON bodies with `Content-Type: application/json`. After activation,
HTTP API requests require `Authorization: Bearer <JWT>`.

For password-protected rooms, include `password` in activation. Persist a random
`resume_credential` before the first activation request so retries reuse the same
participant slot. See [invites and chats](../README.md#invites-and-chats) for
room limits, password handling, expiration, and browser use.

## Bundled test web client

`/client` exchanges encrypted messages using the unaudited [v1 wire format](PROTOCOL.md)
on localhost or HTTPS. It is not compatible with NaCl `crypto_box` without an
interoperability layer. See [invites and chats](../README.md#invites-and-chats) for
use/reset behavior and [SECURITY_REVIEW.md](SECURITY_REVIEW.md) for trust boundaries.
`/l/{token}` is the invite landing page and links to `/client?invite={token}`.
Incoming group messages show the sender's `display_name` from the chat roster
alongside `sender_public_id`, falling back to the public ID when unnamed. The
client saves this label in encrypted local history; message envelopes are unchanged.
Existing history uses current roster names when saved sender routing metadata is
available. Reload the client to load the updated script URL.
After reload, restored history opens at the latest messages even if server
synchronization fails. Incoming messages preserve the position when reading older history.
Display names are not authenticated by the encryption protocol.

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
  "public_key": "<base64, 32 bytes, X25519>",
  "display_name": "Alice",
  "resume_credential": "<43-character base64url credential>",
  "session_credential": "<separate 43-character base64url credential>"
}
```

This is the only activation route. The former
`POST /api/v1/links/{token}/activate` route is removed and returns 404; all clients
must send the invite secret in the JSON body. The invite landing paths
`/l/{token}` and `/client?invite={token}` remain unchanged and still require URL
log protection.

`display_name` is optional; include `password` for protected rooms. The response
contains a client JWT, user identity, and chat participants. `session_credential`
opts into [renewable sessions](#renewable-client-sessions), adding the session ID
and deadline fields described there; omitting it uses legacy activation until sunset.

```text
{
  "token": "<client JWT>",
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
reconnects remain available. Expired or revoked access cannot be restored with
a password or resume credential.

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
   The `hello` frame includes backlog; `message` and `read` events follow live.
   Legacy `/ws?token=<JWT>` and `sideword.auth.<JWT>` subprotocol authentication
   remain supported, but can expose credentials to URL/header logs.

2. **Polling fallback**:

   ```
   GET /api/v1/poll
   Authorization: Bearer <JWT>
   ```

   Returns `messages` and `read_receipts` arrays.

## Exact delivery identities

Every message and receipt returned by polling, WS backlog, or live WS events has
an additive `delivery_id`: a server-generated, random 32-character lowercase hex
identifier. It stays stable while that row exists, including database restarts
and backups. A newly queued row gets a fresh identity even if SQLite reuses its
integer `id` or a sender repeats its `client_message_id`. It is metadata, not a
secret or proof of decryption. All operations still require the owner's JWT.

Use the exact endpoints below for new clients. The bundled browser uses them
without falling back to legacy deletion if an older server rejects the request.

## Mark read

```
POST /api/v1/chats/7/read/exact
{
  "messages": [{
    "delivery_id": "0123456789abcdef0123456789abcdef",
    "chat_id": 7,
    "sender_public_id": "sender-public-id",
    "client_message_id": "m-1"
  }]
}
```

Copy all reference fields from the received message. All must match a row
addressed to the authenticated recipient; `chat_id` must also match the route.
Send 1–100 references per request. Deletion and receipt creation occur in one
transaction. Concurrent readers or retries after a lost response consume a row
at most once. The response is `{"marked": N}`; stale references return zero.
The sender receives the receipt through poll/WS and can then acknowledge it.

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

`messages` accepts the same references as exact read, deleting ciphertext without
creating receipts. `receipts` binds the delivery, chat, reader, and client message
ID to the authenticated original sender. Both lists are optional and limited to
100 items each; unknown fields are rejected. The response is
`{"deleted_messages": N, "deleted_receipts": N}`. Repeat a failed request with
the same references; a successful retry can report zero if the first committed.

After ACK, rows are gone from the server. SQLite row IDs may be reused: deduplicate
messages by chat, sender, and client message ID; receipts by chat, reader, and
client message ID. Treat timestamps without a timezone as UTC when ordering history.

Legacy `POST /api/v1/ack` with `message_ids`/`read_ids` and
`POST /api/v1/chats/{chat_id}/read` with `client_message_ids` remain supported.
They retain their ambiguous matching: delayed ACKs can target reused row IDs,
and legacy reads can match different group senders sharing a client message ID.
Set `SIDEWORD_ALLOW_LEGACY_ACK=false` after migrating clients to return 410
from legacy deletion endpoints. Sends now deduplicate within the retry window
described below. Use a fresh client message ID for each logical message. Stolen bearer tokens can still
delete their owner's deliveries through either API.

## Drop own undelivered messages

```
DELETE /api/v1/chats/{chat_id}/outbox
```

Deletes pending ciphertext your user sent that recipients have not read yet.

## Message security model

The server contract requires a 32-byte base64 X25519 public key and treats each
ciphertext envelope as opaque base64 data. Production clients must agree on an
authenticated envelope format, such as the bundled [v1 contract](PROTOCOL.md).
The server's `key_fingerprint` is a convenience, not a trust anchor: calculate and
verify fingerprints locally. V1 has no recipient forward secrecy or recovery
after key compromise. See [threat model and limits](SECURITY_REVIEW.md#threat-model-and-limits)
for relay, browser, bearer-token and retention boundaries.

## Send retries and capacity

`POST /api/v1/chats/{chat_id}/messages` is idempotent by chat, authenticated sender, and
`client_message_id` for `SEND_IDEMPOTENCY_DAYS` (default 30). Identical decoded
ciphertext and recipient sets return the original 200 response; conflicting
payloads return 409. An identical retry after read/ACK or partial fanout delivery
does not recreate deleted rows. Recipient order and equivalent base64 encoding
do not change the comparison. IDs must be unique for each logical message.
The guarantee requires a ledger record from the first successful upload;
pre-upgrade sends without that evidence have no retroactive retry guarantee.

`/me.send_retry_window_seconds` advertises the current window. Persist envelopes
before uploading; never re-encrypt a retry under the same ID. A shorter later
configuration must not be assumed to extend old records. Beyond the saved window,
verify delivery manually. The browser retains expired retries for review/discard.
Server outbox deletion also preserves retry evidence. A complete database backup
preserves the ledger; configuration exports do not. Restoring an older backup can
lose evidence of later sends and replay later refresh credentials.

Requests are limited to 2 MiB by default, envelopes to 100 per send, and poll/WS
backlog to 100 messages plus 100 receipts. Poll and consume batches until empty.
A sender may have 1,000 outstanding envelopes/receipts and 16 MiB queued
ciphertext, with at most 60 new sends/minute; identical retries consume no new
capacity. Global queue, byte, receipt, participant and ledger quotas return 429 without
partial fanout or eviction. Default request rate is 600/minute/IP/process;
WS supports 256 connections/process, 4/user/process, 4 KiB inbound frames and
120 inbound frames/minute/socket. Proxy limits must complement these counters.

## Renewable client sessions

On activation, optionally include `session_credential`: 32 random bytes encoded
as 43 unpadded base64url characters, persisted before the request. The response
adds `session_id`, `access_expires_at`, and `session_expires_at`. Identical initial
credentials can retry a lost activation response before their first rotation.
The browser opts in. A valid legacy JWT can instead POST `/api/v1/sessions` with
`{"credential":"<persisted-random-secret>"}` to migrate; registered sessions
cannot use that endpoint to extend their absolute lifetime.
Issuance rechecks the current participant identity, active state, and invite under
the database write reservation, including retries of an existing credential.
Migration also rechecks the legacy JWT expiry and configured sunset at that point.
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
New session lifetimes are capped by the invite expiry at issuance. Activation,
migration and refresh responses cap both deadline fields by the current invite
expiry, including sessions created by earlier releases. Extending an invite does
not extend a session's stored lifetime.

GET `/api/v1/sessions` lists the authenticated user's sessions; DELETE
`/api/v1/sessions/{session_id}` revokes one. Revocation affects HTTP and WS session
checks. `/me` reports `access_expires_at` and `session_expires_at`; access ends at
the earliest JWT, session or invite deadline. Consumed/sealed invites still permit valid session refresh;
revoked, deleted, expired invites and inactive users do not.

An optional UTC `LEGACY_TOKEN_DEADLINE` rejects old JWTs and activation without a
session credential after that date. Until then, legacy JWTs still authorize
operations independently of per-device session revocation. Invite resume secrets
also remain separate: revoke the invite if those credentials are compromised.

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
Old stateless admin JWTs require login again. Administrator/schema endpoints
are local-only by default; HTTPS is required outside loopback for all APIs.
