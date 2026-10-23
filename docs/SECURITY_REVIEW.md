# Security review

**Reviewed:** 2026-10-23 · **Backend:** 0.13.2

Maintainer source/test review, not an independent security or cryptographic audit.
Report vulnerabilities through [SECURITY.md](../SECURITY.md).

## Controls and supporting evidence

| Area | Protection | Evidence |
| --- | --- | --- |
| Admission | Body activation, HTTPS, scrypt passwords, persistent guess limits and serialized capacity | [Invite tests](../tests/test_invite_protection.py) |
| Sessions | Registered JWTs, hashed rotating refresh credentials, replay revocation, absolute deadlines and HTTP/WS revalidation | [Session tests](../tests/test_security_stages.py), [boundaries](../tests/test_client_session_boundaries.py) |
| Administration | Bcrypt/72-byte limit, CSRF/origin checks, secure cookies, throttles and reset/logout revocation | [Dependency tests](../tests/test_dependency_security.py), [login races](../tests/test_admin_login_races.py) |
| Authorization | Membership checks, per-socket validation, closure enforcement and restricted public listener | [Closure](../tests/test_conversation_closure.py), [listener](../tests/test_shared_listener.py), [aliases](../tests/test_cross_origin_delivery.py) |
| Encryption/storage | P-256/HKDF/AES-GCM, non-exportable keys, first-use pins, encrypted history/outbox and persistence before ACK | [Protocol](PROTOCOL.md), [crypto](../tests/client_crypto.test.cjs), [delivery](../tests/client_delivery.test.cjs) |
| Delivery/deletion | Owner-bound exact references, atomic receipts, retry ledger and UTC ordering | [Exact delivery](../tests/test_exact_delivery.py), [status](../tests/test_message_status.py) |
| Responses/capacity | CSP, text-only rendering, no-store/no-referrer, redacted validation and bounded requests/queues | [Guards](../app/guards.py), [security tests](../tests/test_security_stages.py) |
| Build/deployment | Exact-commit gates, immutable images, non-root runtime, schema/health preflight, OIDC, locks and backups | [CI](../.github/workflows/test.yml), [scan tests](../tests/test_security_scanning.py), [deployment tests](../tests/test_deploy.py) |

## Threat model and limits

- The relay sees identities, memberships, timing, sizes and confirmations. It cannot
  verify third-party encryption or real-world identity.
- V1 has no forward secrecy, post-compromise recovery or ratchet. A stolen identity
  key exposes recorded inbound messages. First-use pins cannot authenticate initial
  keys or group membership.
- Non-exportable keys do not protect against malicious same-origin code or a
  compromised device. TLS terminators, served code and admission endpoints are trusted.
- Bearer tokens permit routing/deletion without proving message-key possession.
  Theft can disrupt delivery without decrypting messages. Resume credentials
  survive session revocation; see [access lifecycle](SESSION_LIFECYCLE.md).
- Presence/receipts are assertions, not proof of attention or encryption. Revocation
  cannot retract sent data. Independent workers do not share connections; process
  limits do not provide distributed denial-of-service protection.
- TLS/proxy trust, IAM, logs, secrets and resource isolation require operator
  verification. Follow [deployment controls](UPGRADING.md#public-deployment-controls).

## Delivery, local storage and retention

Queued ciphertext is deleted on durable ACK or TTL, never viewing alone. Receipt,
retry and membership metadata have separate lifetimes; cleanup runs at startup and
hourly. See [retention](API.md#message-status-recovery).

SQLite deletion does not erase WAL/free pages/backups. Exports are unencrypted and
contain admission secrets/verifiers. Browser history/outboxes are encrypted, but
device credentials are separate. Lost origin-local keys are unrecoverable from
server backups. [Restoring backups](UPGRADING.md#restore-safely) can revive credentials.

## Prioritized findings and improvements

P1: resolve before security-sensitive use. P2: planned hardening; priorities do not
establish remote exploitability.

| Finding | Remaining action |
| --- | --- |
| F-01 · Medium · P1 | Obtain independent protocol review. Stronger secrecy/group claims require a reviewed ratchet/group design and interoperability/recovery evidence; old messages gain no retroactive protection. |
| F-03 · Medium · P1 | Redact admission credentials at every logging layer, protect exports/copies, share passwords separately and revoke leaks. Body activation/no-referrer do not erase URL history or logs. |
| F-04 · Low · P2 | Define host backup/rollback-image retention, test restores and monitor disk. [Registry cleanup](../scripts/artifact-cleanup.json) does not prune host copies. |
| F-06 · Low · P2 | Dependency ranges and numbered tags may resolve differently. Preserve checked image digests and monitoring; Action tags remain numeric by policy. |

## Automated advisory monitoring

[CONTRIBUTING](../CONTRIBUTING.md#security-checks) owns scanner policies, schedules,
reports and GitHub settings. Secret Scanning and Push Protection were verified
enabled. CodeQL default setup was unconfigured; keep it disabled to avoid duplicates.
The last verified ruleset requires smoke/container. CodeQL enforcement is configured
in CI; its first hosted promotion-gate run remains pending.

## Verification record and gaps

- All 542 isolated Python cases passed; existing 195 JavaScript cases remain valid
  for unchanged code. Ruff, compilation, workflow lint and documentation links passed.
- Python 3.12.15/Alpine 3.24.3 images passed Trivy 0.76.1 on amd64 (emulated) and
  arm64 (native): zero findings, including unfixed advisories. Exact amd64 runtime
  inventory (33 distributions) passed pip-audit. Results reflect that advisory database.
- Alpine amd64 regression tests preserved production dependency versions. Both
  architectures passed imports, UID 1000/read-only-root, health, admin reset and
  persistence checks. Dockerfile configuration scanning found no issues.
- CodeQL replay allowed the actual Medium finding and blocked synthetic High,
  failed-analysis and missing-report cases. Tests cover severity boundaries,
  query-pack metadata and existing findings. Deployment tests reject failed gates,
  obsolete/disabled probes and incompatible databases before stopping the backend.
- The metadata probe passed against the downloaded image without network or
  production data. Hosted CI passed for 0.13.2. Approved VM recovery verified that
  same image healthy on both local listeners, with public HTTP/WS administration
  blocked. The old database and configuration/database backups remain preserved.

One hosted Medium `py/url-redirection` alert remains in admin bulk deletion.
Source review confirms only `users`, `links` and `chats` reach the redirect;
other resources return 404. The alert has not been suppressed.

Unverified: browser/device storage, native-client interoperability, full production
proxy/IAM/log/secret/resource controls, operator-backup restore, bundled Action
advisories and independent cryptographic review. Local tests do not prove these.
Release history belongs in [CHANGELOG.md](../CHANGELOG.md).
