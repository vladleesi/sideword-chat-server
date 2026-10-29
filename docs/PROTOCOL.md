# Browser test-client P-256 protocol v1

Unaudited; no recipient forward secrecy or post-compromise recovery. Backend, HTTP
API, application payload and cryptographic envelope versions are independent.
`client-protocol.js` owns pure Web Crypto/payload operations; `client.js` owns peer
trust, DOM, networking and persistence. See [security limits](SECURITY_REVIEW.md).

## Wire representation

API `ciphertext` is standard base64 of UTF-8 envelope JSON:

```json
{"v":1,"alg":"P256-2DH-HKDF-SHA256-AES256GCM","epk":"<base64 65-byte point>","salt":"<base64 16 bytes>","iv":"<base64 12 bytes>","ct":"<base64 ciphertext + 16-byte tag>"}
```

Use uncompressed SEC1 P-256 points (`0x04 || X || Y`, 32-byte coordinates); validate
curve membership and reject compressed/other-curve keys. For each recipient:

1. Generate a fresh ephemeral ECDH pair, random 16-byte salt and 12-byte IV with CSPRNG.
2. Concatenate `DH(sender_identity_private, recipient_public) || DH(ephemeral_private,
   recipient_public)` (64 bytes); recipient derives equivalents with its private key.
3. HKDF-SHA-256 derives an AES-256-GCM key using that secret, salt and UTF-8 info:
   `sideword-web-p256-v1|{chat_id}|{client_message_id}|{sender_public_id}|{recipient_public_id}`.
   Use decimal chat ID and exact API strings.
4. Encrypt UTF-8 plaintext with that key/IV and the same context as AAD; append a
   128-bit GCM tag. Discard derived secrets/ephemeral keys; JS cannot guarantee erasure.

Reject unsupported formats/algorithms, invalid DH and authentication failures without
plaintext/weaker fallback. Context does not authenticate names, timestamps or group
membership. Groups use separate per-recipient envelopes, never a shared group key.

## Application payload v1

Plaintext JSON has exactly one of these field sets:

```json
{"v":1,"type":"text","text":"Hello"}
{"v":1,"type":"attachment","name":"example.bin","mime":"application/octet-stream","size":3,"data":"AP8B"}
```

- Text: 1–16,000 UTF-8 bytes; arbitrary user text always goes inside `text`.
- Attachment: `size` integer 0–1,048,576; `data` canonical padded standard base64,
  decoded length exactly `size`; nonempty `name` at most 255 UTF-16 code units;
  `mime` 0–127 printable ASCII characters, including empty.
- Reject unknown fields/types/versions, malformed encodings, size mismatches and
  payloads over 1,400,000 UTF-8 bytes. No legacy/raw-text fallback.

Names, declared types and file bytes are encrypted. One file per message; select,
paste or drop, review, then Send. No captions, previews, playback, compression or
chunking. Read no bytes before size validation/peer checks; never fetch pasted URLs.

### Encoding and resource budget

Maximal file with maximally escaped metadata:

| Stage | Bytes/ASCII characters |
| --- | --- |
| Original | 1,048,576 |
| File base64 | 1,398,104 |
| Payload JSON | 1,399,960 |
| GCM output + tag | 1,399,976 |
| Envelope JSON | 1,866,845 |
| Outer base64 per recipient | 2,489,128 |

Base64 length is `4 * ceil(n/3)`; envelope fields add 209 bytes plus base64 ciphertext.
The conservative four-recipient HTTP bound is 9,959,328 bytes, below 10 MiB; five
maximal copies exceed it. Smaller files support up to 100 recipients, within quotas.
The relay bounds opaque resources, not verified plaintext size/type. See [API limits](API.md#send-retries-and-capacity).

Encrypt recipients sequentially; retained envelopes still multiply bandwidth/storage.
File buffers, JSON/base64, crypto/storage copies and possibly two-byte JS strings
coexist: wire limits are not heap limits. Outbox: 100 records/32 MiB encrypted bytes;
history depends on browser quota. Storage failures withhold ACK and preserve retries.
Invalid message work is cooled down for 60 seconds in a bounded cache; reload may retry sooner.

### Downloads and confirmations

Render names with `textContent`. Suggested downloads remove path separators, controls,
bidi formatting and leading/trailing dots/spaces, capped at 180 UTF-16 units with
`download` fallback. Keep original metadata encrypted in history.

Explicit clicks create generic `application/octet-stream` Blobs and object URLs with
the `download` attribute. Never embed file contents or auto-open/download. Revoke URLs
after a 60-second handoff or cleanup; at most eight live URLs. Browser/OS can refuse or
rename downloads. No server malware inspection; saved downloads are ordinary local
files that cannot be remotely revoked. Viewing confirms the card, not file opening.

## Identity checks and local state

Compare full lowercase hex SHA-256 fingerprints of raw public points through another
trusted channel. First-use pins bind peer ID/local key/origin; atomic writes prevent
tab overwrites, and mismatches block sending/decryption. Pins cannot authenticate
initial substitution, a malicious roster or served code. Names grant no trust.

IndexedDB `sideword-test-client-p256-payload-v1` v1, store `device`, holds non-exportable
identity/history CryptoKeys. History/outboxes use separate AES-256-GCM, fresh 12-byte IVs
and full storage key as AAD; no plaintext/key export fallback. Origins have independent
state. Preserve unreadable keys/history and fail admission/renewal closed on storage or
identity mismatch; never silently replace keys. See [devices](SESSION_LIFECYCLE.md#persistence-devices-and-reconnects).

## Delivery requirements for future clients

- Serialize WS/poll receipt processing. Authenticate, check pins, decrypt, validate and
  durably persist locally **before** exact deletion ACK; keep failures retryable.
- Deduplicate by chat/sender/client message ID, not reusable SQLite row IDs. Receipts
  also bind reader/original delivery ID/stage. Normalize timestamps to UTC.
- Persist immutable outgoing envelopes before upload and retry identical IDs/ciphertext
  within the [retry window](API.md#send-retries-and-capacity), never re-encrypting.
- Encrypt acceptance/original recipients, confirmations, viewing intent and ACK markers.
  Hash event storage keys to hide recipient bindings; merge independent recipients/tabs
  without lost updates. ACK each receipt's own exact reference after persistence.
- Confirm viewing only after actual visibility and prior durable delivery; neither
  rendering/download nor presence substitutes for [confirmation rules](API.md#durable-delivery-and-actual-viewing).
- Respect closure/expiry without erasing history or automatically resending. Supported
  backups retain retry metadata but omit queued content; relay IDs do not prove decryption.

See [verification gaps](SECURITY_REVIEW.md#verification-record-and-gaps).
