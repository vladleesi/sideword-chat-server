# Browser test-client P-256 protocol v1

This documents the current test format for interoperability, not a recommendation
to build a new cryptographic protocol. It is unaudited, has no recipient forward
secrecy or post-compromise recovery, and is not the Signal Protocol. See the
[security review and migration plan](SECURITY_REVIEW.md).

The backend's `/api/v1` version, backend release version, and ciphertext version
are independent. The backend handles opaque ciphertext, never message plaintext
or private keys. Clients using different envelope formats cannot interoperate
just because they use the same HTTP API.

Backend 0.6.0 replaces the prior test format completely. This format retains
`v: 1` with a distinct P256 algorithm identifier and authenticated context;
there is no X25519 encryption/decryption support or algorithm negotiation.
Old device credentials, rooms and configuration exports containing retired keys
are incompatible. Start new rooms with new invites and fresh P-256 identities.

## Implementation boundary

`app/static/client-protocol.js` implements the existing wire operations with
Web Crypto and explicit identity/peer inputs. It has no network, DOM, session,
or storage access. `client.js` owns peer pinning, identity/history persistence,
delivery queues, ACKs, sessions, and rendering. The protocol module alone does
**not** enforce trust: callers must authenticate the supplied peer key first.
Protocol failures use `ProtocolError` with fixed explanations. The UI distinguishes
these from browser storage, login and HTTP failures; raw exception messages and
unrecognized server details are not rendered.

## Wire representation

The `ciphertext` API field is standard base64 of UTF-8 JSON:

```json
{
  "v": 1,
  "alg": "P256-2DH-HKDF-SHA256-AES256GCM",
  "epk": "<base64 uncompressed ephemeral P-256 point, 65 bytes>",
  "salt": "<base64 random salt, 16 bytes>",
  "iv": "<base64 random IV, 12 bytes>",
  "ct": "<base64 ciphertext followed by 16-byte GCM tag>"
}
```

Public identity keys are standard base64 of uncompressed SEC1 P-256 points:
`0x04 || X || Y`, with two 32-byte big-endian coordinates (65 bytes total).
Both backend admission and native Web Crypto imports validate curve membership;
compressed points and other curves are rejected. Keys use ECDH with `namedCurve: "P-256"`.
For each recipient and message, the sender generates a fresh ephemeral P-256
key pair, 16-byte random salt, and 12-byte random IV using the platform CSPRNG.
Given sender identity key `S`, recipient identity key `R`, and ephemeral key `E`:

1. Compute `DH(S_private, R_public) || DH(E_private, R_public)` in that order
   (64 bytes total). The receiver computes the same values using `R_private`.
2. Define `context` as UTF-8 of
   `sideword-web-p256-v1|{chat_id}|{client_message_id}|{sender_public_id}|{recipient_public_id}`.
   Chat IDs use decimal text; IDs otherwise use their exact API strings.
3. Derive a 32-byte key with HKDF-SHA-256: input key material is the concatenated
   DH outputs, salt is the envelope salt, and info is `context`.
4. Encrypt the UTF-8 message with AES-256-GCM, the envelope IV, `context` as
   associated data, and a 128-bit authentication tag. Append the tag to ciphertext.
5. Discard ephemeral private keys and derived secrets after use. JavaScript
   garbage collection and buffer overwrites do not guarantee physical erasure.

Reject unsupported versions/algorithms and failed authentication. Do not retry
them using a weaker algorithm. Web Crypto rejects invalid DH operations; never
replace a rejected shared secret with zeros or fall back to plaintext. The v1
context does not authenticate display names, server timestamps, or a group roster.

Groups use independent pairwise envelopes for every other member, under the same
client message ID. They have no shared group ratchet or authenticated membership
epochs. The server requires envelopes to cover all peers.

## Identity checks and local state

The fingerprint is lowercase hex SHA-256 of the raw public key (64 characters).
Calculate it locally, and compare the full value and intended participant with
the key's owner over another trusted channel. The displayed fingerprints are
not a Signal safety number and do not imply a verified identity.

The test client stores the first fingerprint for each peer public ID, scoped to
the local device public key and browser origin. Atomic IndexedDB transactions
prevent competing tabs from overwriting a pin. Subsequent mismatches block use
of that key. A newly assigned public ID is a new trust decision, including after
a peer resets their device. Pinning cannot detect substitution before first use,
a malicious initial roster, or malicious JavaScript served by the origin.

The IndexedDB database is `sideword-test-client-p256`, version 1, store `device`.
The prior database is left untouched and is never read by this client. It is
not a compatibility or recovery path. New identities require fresh peer
fingerprint verification. The encrypted `history:` format within this database
uses a separate non-exportable AES-256-GCM
key, a fresh 12-byte IV per record, and the complete history storage key as AAD.
Private identity and storage keys are browser CryptoKeys, never uploaded. New
private identity and ephemeral keys are generated non-exportable directly;
there is no private-key export, wrapping, or plaintext storage fallback.
Before invite admission, the client reads the saved identity in a separate
transaction and checks that both device keys and the expected participant/public
key are restored. Identity reads settle only after transaction completion.
An absent record is distinguished from a null deserialization result or a null
key field. Unreadable records are retained and writes that would replace them
are aborted. Missing or unreadable device storage blocks renewal without being
reported as a concurrent device change; an actual changed participant/public key
still requires a reload.

P-256 removes this client's dependency on the key type affected by the reported
[WebKit persistence bug](https://bugs.webkit.org/show_bug.cgi?id=312279).
The preflight still detects failed storage round trips before admission. It cannot
repair browser storage or recover inaccessible keys. Startup failures remain visible
on the activation form. Storage and platform behavior still require device QA.

History stores participant labels alongside routing identities. Current roster
names can update displayed labels without changing stored history, delivery IDs
or acknowledgements; records without a matching roster keep their saved labels.
Display names are server-provided metadata, unauthenticated by the envelope.

## Delivery requirements for future clients

Optional participant presence is a separate, unencrypted connection-status event;
it does not change message envelopes, receipts, key checks or delivery ACKs.
See the [presence contract](API.md#participant-presence) for opt-in authentication,
heartbeat deadlines, membership scoping and mandatory unknown/expiry behavior.

Authenticate, decrypt, and commit local history before `/ack/exact` with
`confirm_delivery:true`, which removes ciphertext and confirms durable delivery.
Use `/read/exact` with `viewed:true` only after actual viewing; it never requires
retained ciphertext. See [delivery and viewing](API.md#durable-delivery-and-actual-viewing)
for visibility, authorization and backward compatibility. Failed decryption, pin checks, or
storage must leave messages retryable. Serialize incoming processing across WS
and polling. Deduplicate messages by chat, sender public ID, and client message
ID; receipt identities also include the original message delivery ID and stage. SQLite row IDs
are not durable message identities. Treat naive database timestamps as UTC.

Use the [exact delivery endpoints](API.md#exact-delivery-identities), binding
`delivery_id`, chat, sender/reader public ID, and client message ID. The random
delivery identity is relay metadata, outside the v1 ciphertext and HKDF context.
It identifies one queued row and survives a full database backup/restore. Batch
at most 100 references per list; retain failed batches for retry. A zero count
is a successful no-op when the referenced delivery no longer exists. Legacy exact read
consumes a row and creates its receipt atomically. Distinct delivery/viewing
confirmations update the bounded ledger atomically and are idempotent across
concurrent requests. Store acceptance, per-recipient receipts, viewing intent and
ACK markers as encrypted history metadata, not timeline messages. Legacy receipt
entries remain available as hidden delivery evidence; they cannot confirm viewing.

The legacy `/ack` takes row IDs and `/read` takes client message IDs. Delayed
ACKs can race row-ID reuse, and colliding client message IDs from different group
senders are ambiguous to legacy `/read`. They remain supported for existing
clients; new clients must not fall back to them. Persist outgoing ciphertext before
upload and retry the same envelopes within the [server retry window](API.md#send-retries-and-capacity).
Delivery IDs do not authenticate relay metadata or prevent malicious relay replay.
The browser uses an encrypted outbox and Web Locks across tabs; browser-independent
interoperability still requires release QA.

`tests/client_crypto.test.cjs` checks both directions of P-256 interoperability
against Node's independent OpenSSL APIs using synthetic fixed keys, salt, and
IV, plus group isolation, tampering, malformed keys, non-exportable ephemeral keys,
and the encrypted local-history format.
