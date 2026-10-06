# Browser test-client protocol v1

This documents the existing format for interoperability, not a recommendation
to build a new cryptographic protocol. It is unaudited, has no recipient forward
secrecy or post-compromise recovery, and is not the Signal Protocol. See the
[security review and migration plan](SECURITY_REVIEW.md).

The backend's `/api/v1` version, backend release version, and ciphertext version
are independent. The backend handles opaque ciphertext, never message plaintext
or private keys. Clients using different envelope formats cannot interoperate
just because they use the same HTTP API.

## Implementation boundary

`app/static/client-protocol.js` implements the existing wire operations with
Web Crypto and explicit identity/peer inputs. It has no network, DOM, session,
or storage access. `client.js` owns peer pinning, identity/history persistence,
delivery queues, ACKs, sessions, and rendering. The protocol module alone does
**not** enforce trust: callers must authenticate the supplied peer key first.

## Wire representation

The `ciphertext` API field is standard base64 of UTF-8 JSON:

```json
{
  "v": 1,
  "alg": "X25519-2DH-HKDF-SHA256-AES256GCM",
  "epk": "<base64 ephemeral X25519 public key, 32 bytes>",
  "salt": "<base64 random salt, 16 bytes>",
  "iv": "<base64 random IV, 12 bytes>",
  "ct": "<base64 ciphertext followed by 16-byte GCM tag>"
}
```

Public identity keys are standard base64 of raw 32-byte X25519 public keys.
For each recipient and message, the sender generates a fresh ephemeral X25519
key pair, 16-byte random salt, and 12-byte random IV using the platform CSPRNG.
Given sender identity key `S`, recipient identity key `R`, and ephemeral key `E`:

1. Compute `DH(S_private, R_public) || DH(E_private, R_public)` in that order
   (64 bytes total). The receiver computes the same values using `R_private`.
2. Define `context` as UTF-8 of
   `sideword-web-v1|{chat_id}|{client_message_id}|{sender_public_id}|{recipient_public_id}`.
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

Existing IndexedDB database `sideword-test-client`, version 1, and store `device`
are retained. Peer pins and session/invite fields are additive; the encrypted
`history:` format is retained. Local history uses a separate non-exportable AES-256-GCM
key, a fresh 12-byte IV per record, and the complete history storage key as AAD.
Private identity and storage keys are browser CryptoKeys, never uploaded. New
private keys are generated non-exportable; existing keys are reused unchanged.
The bundled client stores incoming group sender labels with each new history
entry, using the roster's display name and public ID, or just the ID if unnamed.
When a roster is available, history renders current sender labels using the
saved sender public ID or the original composite incoming identity. Legacy records
without that identity and records without a matching roster keep their saved labels.
This does not rewrite stored history or change delivery identities.
Names are server-provided metadata,
not authenticated by the encrypted envelope.
History rendering waits until the client panel is visible so its first render
can scroll to the latest message, including when initial synchronization fails.
Later incoming delivery preserves the position when reading older history.

## Delivery requirements for future clients

Authenticate, decrypt, and commit local history before issuing either `/read/exact`
or `/ack/exact`; both can delete queued ciphertext. Failed decryption, pin checks, or
storage must leave messages retryable. Serialize incoming processing across WS
and polling. Deduplicate messages by chat, sender public ID, and client message
ID, and receipts by chat, reader public ID, and client message ID. SQLite row IDs
are not durable message identities. Treat naive database timestamps as UTC.

Use the [exact delivery endpoints](API.md#exact-delivery-identities), binding
`delivery_id`, chat, sender/reader public ID, and client message ID. The random
delivery identity is relay metadata, outside the v1 ciphertext and HKDF context.
It identifies one queued row and survives a full database backup/restore. Batch
at most 100 references per list; retain failed batches for retry. A zero count
is a successful no-op when the referenced delivery no longer exists. Exact read
consumes a row and creates its receipt atomically, including concurrent requests.

The legacy `/ack` takes row IDs and `/read` takes client message IDs. Delayed
ACKs can race row-ID reuse, and colliding client message IDs from different group
senders are ambiguous to legacy `/read`. They remain supported for existing
clients; new clients must not fall back to them. Persist outgoing ciphertext before
upload and retry the same envelopes within the [server retry window](API.md#send-retries-and-capacity).
Delivery IDs do not authenticate relay metadata or prevent malicious relay replay.
The browser uses an encrypted outbox and Web Locks across tabs; browser-independent
interoperability still requires release QA.

`tests/client_crypto.test.cjs` checks both directions of v1 interoperability
against Node's independent OpenSSL APIs using synthetic fixed keys, salt, and
IV, plus group isolation, tampering, and the legacy local-history format.
