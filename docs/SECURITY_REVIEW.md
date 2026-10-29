# Security review

**Reviewed:** 2026-10-28 · **Backend:** 0.16.0. Maintainer source/test review;
not an independent security or cryptographic audit. [Report vulnerabilities](../SECURITY.md).

## Controls and supporting evidence

| Area | Protection / evidence |
| --- | --- |
| Admission | HTTPS, validated P-256 keys, scrypt passwords, serialized slots and persistent guess limits; [tests](../tests/test_invite_protection.py) |
| Authorization | Live registered sessions, refresh rotation/replay revocation, membership/closure checks across HTTP/WS, public admin blocking; [sessions](../tests/test_security_stages.py), [closure](../tests/test_conversation_closure.py), [listener](../tests/test_shared_listener.py) |
| Encryption | P-256/HKDF/AES-GCM, non-exportable keys, peer pins and encrypted local history/outbox; [protocol](PROTOCOL.md), [crypto](../tests/client_crypto.test.cjs) |
| Delivery | Exact owner-bound references, atomic delivery receipts, immutable retries and persistence before ACK; [tests](../tests/client_delivery.test.cjs), [status](../tests/test_message_status.py) |
| Attachments | Strict payload/size validation, ciphertext-only relay, gesture-only paste/drop and safe explicit downloads; [client](../tests/client_attachments.test.cjs), [relay](../tests/test_attachments.py) |
| Capacity/browser | Bounded requests/queues, CSP, text-only rendering, no-store, CSRF and redacted errors; [guards](../app/guards.py), [tests](../tests/test_security_stages.py) |
| Build/deployment | Exact-image gates, non-root images, recoverable pre-1.0 resets and verified cleanup; [CI](../.github/workflows/test.yml), [deployment tests](../tests/test_deploy.py) |

## Threat model and limits

- Relay sees identities, memberships, routing, timing, sizes and confirmations. It cannot
  verify plaintext file size/type, third-party encryption or real-world identity, and
  performs no malware inspection of encrypted files. Filenames/types are untrusted.
- V1 has no recipient forward secrecy, ratchet or post-compromise recovery. A stolen
  identity key exposes recorded inbound messages. First-use pins cannot authenticate
  initial keys, rosters or malicious served code; compromised devices remain a risk.
- TLS terminators and served code are trusted. Non-exportable keys do not stop same-origin
  malicious code. Bearer theft permits queue deletion/disruption without decryption proof.
- Names do not unlink identities. Presence/receipts are assertions, not proof of attention.
  Revocation cannot retract sent data. Process limits are not distributed DoS protection.
  See [access lifecycle](SESSION_LIFECYCLE.md) and [operator controls](UPGRADING.md#public-deployment-controls).

## Delivery, local storage and retention

Durable local persistence permits ACK; downloads are optional. ACK removes recipient
ciphertext immediately. Unacknowledged content expires after seven days by default;
delivery stops at the fixed deadline and cleanup runs at startup/hourly.
SQLite uses overwrite-on-delete, full synchronization and truncated rollback journals;
WAL is rejected. [Deletion tests](../tests/test_secure_deletion.py) check ordinary file
remnants, backup exclusion and retry/restart behavior. Supported backups omit queued
content/free pages while retaining bounded retry/receipt metadata and credentials.
This is best-effort deletion, not guaranteed physical erasure; see [backups](UPGRADING.md#backups).

Local history/outboxes remain encrypted. Downloads are ordinary local files that
revocation/deletion cannot erase. Card viewing does not confirm file opening.

## Prioritized findings and improvements

| Priority | Remaining action |
| --- | --- |
| F-01 · Medium · P1 | Independent protocol review; stronger secrecy needs a reviewed ratchet/group design. |
| F-03 · Medium · P1 | Verify credential redaction at every logging layer and protect exports; no-referrer does not erase browser history/logs. |
| F-04 · Low · P2 | Manage untracked backups/rollback images, test restores and monitor disk; automatic cleanup covers inventoried deployment backups only. |
| F-06 · Low · P2 | Preserve checked image digests and advisory monitoring; dependency ranges/numeric Action tags can resolve differently. |

## Verification record and gaps

Synthetic Python/Node regressions cover payload boundaries/tampering, peer mismatch,
authorization, ciphertext persistence, duplicate WS/poll delivery, reload retries,
quota failures withholding ACKs, deletion/expiry, safe downloads and transfer gestures.
Client edge cases cover background ACKs, queued duplicates/retries and focus/storage loss before reads.
Deployment regressions use fake commands and real SQLite to check digest binding,
version-aware resets, interrupted recovery/cleanup, writer locks, path/permission tampering
and redacted failures. Live Docker/VM behavior remains unverified. Failed replacements can
retain the original queue privately until [recovery completes](UPGRADING.md#upgrade).
These do not establish browser/OS behavior, native interoperability or deployed memory,
proxy/IAM/log/backup controls. Independent cryptographic review remains outstanding.

Previous image/advisory/deployment checks were for **0.13.2**, not this release.
One unsuppressed hosted Medium `py/url-redirection` alert remains in admin bulk deletion;
source review limits destinations to users/links/chats. CodeQL gate tests cover failures
and severity handling; hosted enforcement for this release remains unverified.
Scanner policies/settings belong in [Contributing](../CONTRIBUTING.md#security-checks).
