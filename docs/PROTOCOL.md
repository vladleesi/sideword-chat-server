# Browser test-client P-256 protocol v1

This unaudited interoperability format has no recipient forward secrecy or
post-compromise recovery. Backend release, `/api/v1` and envelope versions are
independent; sharing an HTTP API does not imply compatible encryption.

## Implementation boundary

`app/static/client-protocol.js` implements Web Crypto wire operations with explicit
identity/peer inputs, without network, DOM, sessions or storage. `client.js` owns
trust checks, persistence, sessions and delivery. Callers must authenticate peer
keys; protocol failures use fixed `ProtocolError` messages, never raw exceptions.
The backend validates public P-256 points but treats ciphertext as opaque.

## Wire representation

The API's `ciphertext` is standard base64 of UTF-8 JSON:

```json
{
  "v": 1,
  "alg": "P256-2DH-HKDF-SHA256-AES256GCM",
  "epk": "<base64 uncompressed P-256 point, 65 bytes>",
  "salt": "<base64 random salt, 16 bytes>",
  "iv": "<base64 random IV, 12 bytes>",
  "ct": "<base64 ciphertext followed by 16-byte GCM tag>"
}
```

Identity keys encode uncompressed SEC1 `0x04 || X || Y`: 65 bytes, coordinates
32 bytes each, ECDH `namedCurve: "P-256"`. Validate curve membership; reject
compressed points and other curves. For each message/recipient generate a fresh
ephemeral P-256 pair, 16-byte salt and 12-byte IV with the platform CSPRNG.
For sender identity S, recipient identity R and ephemeral E:

1. Concatenate `DH(S_private, R_public) || DH(E_private, R_public)` (64 bytes).
   The recipient obtains the same outputs using `R_private`.
2. Set UTF-8 context to
   `sideword-web-p256-v1|{chat_id}|{client_message_id}|{sender_public_id}|{recipient_public_id}`.
   Use decimal chat IDs and exact API ID strings.
3. HKDF-SHA-256 derives 32 bytes from those DH outputs, envelope salt and context as info.
4. AES-256-GCM encrypts UTF-8 plaintext with that key, IV, context as AAD and a
   128-bit tag appended to ciphertext.
5. Discard derived secrets/ephemeral private keys; JavaScript cannot guarantee physical erasure.

Reject unsupported versions/algorithms, invalid DH and authentication failures.
No negotiation, weaker fallback, zero secret or plaintext fallback is allowed.
Context does not authenticate names, timestamps or group membership. Groups use
separate pairwise envelopes under one client message ID, without a shared ratchet
or authenticated membership epochs; cover every peer.

## Identity checks and local state

Fingerprints are lowercase hex SHA-256 of raw public points (64 characters).
Compute locally and compare the full fingerprint/participant through another
trusted channel; the relay's fingerprint is not a trust anchor.

First-use pins are scoped to peer public ID, local device key and origin. Atomic
IndexedDB writes prevent tabs overwriting pins; later mismatches block that key.
A new public ID requires new verification. Pinning cannot detect initial
substitution, a malicious roster or malicious code served by the origin.

IndexedDB `sideword-test-client-p256`, version 1, store `device` holds non-exportable
identity/history CryptoKeys. `history:` records use a separate AES-256-GCM key,
fresh 12-byte IV and full storage key as AAD. Private/ephemeral keys are generated
non-exportable; no export/wrapping/plaintext fallback exists. Device keys are not uploaded.

Before admission, read saved identity in a separate completed transaction and
verify both keys and expected participant/public key. Distinguish absent records
from null/unreadable keys; preserve unreadable state and abort replacement writes.
Block renewal on storage failure; an actual device identity change requires reload.
These checks cannot recover lost keys or establish device compatibility.

Names are unauthenticated labels stored beside routing IDs. Roster label updates
must not change saved delivery identities or history. Origins have independent
keys/pins/history despite sharing rooms; see [device lifecycle](SESSION_LIFECYCLE.md#persistence-devices-and-reconnects).

## Delivery requirements for future clients

- Authenticate, decrypt, verify pins and commit local history **before** exact ACK
  removes ciphertext. Failed crypto/storage stays retryable; serialize WS/poll processing.
- Deduplicate messages by chat/sender/client message ID; receipts also bind reader,
  original delivery ID and event stage. SQLite row IDs can be reused. Treat naive timestamps as UTC.
- Exact operations bind random delivery ID, chat, sender/reader and message ID;
  batch at most 100 per list. IDs survive full backups but are relay metadata,
  outside ciphertext/HKDF context and not proof against relay replay.
- Persist immutable outgoing envelopes before upload; retry identical ciphertext
  within the [retry window](API.md#send-retries-and-capacity). The browser uses encrypted outboxes and Web Locks.
- Store acceptance, per-recipient confirmations, viewing intent and ACK markers as
  encrypted history metadata. ACK receipts only after persistence; zero counts can mean an earlier retry committed.
- Send viewing confirmations only after actual viewing and prior durable delivery;
  presence is separate connection metadata, never viewing/delivery proof. See
  [confirmation semantics](API.md#durable-delivery-and-actual-viewing).
- Respect conversation closure without erasing local history or re-encrypting
  pending messages. Resume only within ordinary deadlines when reopened.

See [verification gaps](SECURITY_REVIEW.md#verification-record-and-gaps) before
claiming browser/native interoperability or cryptographic assurance.
