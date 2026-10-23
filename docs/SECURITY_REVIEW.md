# Security review

**Reviewed:** 2026-10-22 · **Backend:** 0.13.2

Maintainer source/test review of the ciphertext relay, bundled test client and
CI/deployment controls. This is not an independent audit, penetration test,
cryptographic proof or verification of private production configuration.

## Controls and supporting evidence

| Area | Implemented protection | Evidence |
| --- | --- | --- |
| Admission | Body-based activation, HTTPS outside loopback, scrypt passwords, persistent guess limits and serialized capacity allocation | [Invite tests](../tests/test_invite_protection.py) |
| Client sessions | Registered JWT sessions, hashed rotating refresh credentials, replay revocation and fixed absolute deadlines; HTTP/WS revalidation | [Session tests](../tests/test_security_stages.py), [issuance boundaries](../tests/test_client_session_boundaries.py) |
| Administration | Bcrypt with 72-byte UTF-8 limit, CSRF/origin checks, secure cookies, throttles and reset/logout revocation | [Dependency tests](../tests/test_dependency_security.py), [login races](../tests/test_admin_login_races.py) |
| Authorization | Membership-scoped chat access, per-socket session validation, closure enforcement and private shared listener | [Closure](../tests/test_conversation_closure.py), [listener](../tests/test_shared_listener.py), [alias isolation](../tests/test_cross_origin_delivery.py) |
| Encryption/storage | P-256/HKDF/AES-GCM, non-exportable browser keys, first-use pins, encrypted history/outbox and persistence before ACK | [Protocol](PROTOCOL.md), [crypto](../tests/client_crypto.test.cjs), [delivery](../tests/client_delivery.test.cjs), [identity](../tests/client_identity.test.cjs) |
| Delivery/deletion | Exact owner-bound references, atomic receipts, stable retry ledger and UTC ordering | [Exact delivery](../tests/test_exact_delivery.py), [status](../tests/test_message_status.py) |
| Responses/capacity | CSP, text-only user rendering, no-store/no-referrer, redacted validation, bounded requests/queues/connections | [Guards](../app/guards.py), [security tests](../tests/test_security_stages.py), [static responses](../tests/test_static_delivery.py) |
| Build/deployment | Exact-commit smoke/container gates, verified image IDs/digests, non-root runtime, OIDC, deployment locks/backups and restricted failure output | [CI](../.github/workflows/test.yml), [deployment tests](../tests/test_deploy.py) |

These controls have configuration and trust limits below. Tests use synthetic
identities and isolated databases, not private deployments.

## Threat model and limits

- The relay sees identities, memberships, timing, sizes and confirmations. It
  cannot verify third-party encryption or a participant's real-world identity.
- V1 has no recipient forward secrecy/post-compromise recovery. A stolen identity
  key exposes recorded inbound messages; there is no ratchet. First-use pins
  cannot authenticate initial keys, new IDs or group membership.
- Non-exportable keys do not prevent malicious same-origin code using them or
  reading plaintext. TLS protects transit; the origin, device and password
  admission endpoint/TLS terminator remain trusted.
- Bearer tokens authorize routing/deletion without proving message-key possession.
  Theft can disrupt delivery without decrypting content. Resume credentials
  survive session revocation; invite revocation and account deactivation have
  [different effects](SESSION_LIFECYCLE.md).
- Presence, timestamps and receipts are relay/recipient assertions, not proof of
  identity, human attention or successful encryption. Revocation cannot retract
  already-sent data; independently routed workers do not share live state.
- Process-local request/WS limits are not distributed denial-of-service protection.
  Proxy TLS/trust, route blocking, IAM, logs, secrets, storage and resource limits
  require per-installation verification; see [deployment controls](UPGRADING.md#public-deployment-controls).

## Delivery, local storage and retention

The relay deletes queued ciphertext on durable ACK or TTL (default 30 days),
never on viewing alone. Receipts have their own queued lifetime; bounded send and
confirmation metadata lasts until the later retry/message-retention deadline.
User/invite/membership metadata has a separate lifetime. Cleanup runs at startup
and hourly. See [API retention](API.md#message-status-recovery).

SQLite deletion does not erase WAL/free pages/backups. Database/config exports
are unencrypted; exports contain invite secrets/verifiers. Browser history/outboxes
are encrypted, but device credentials are not covered by history encryption.
Lost origin-local keys cannot be recovered from server backups. Use the
[backup/restore procedure](UPGRADING.md); restore may revive revoked credentials.

## Prioritized findings and improvements

Priorities describe residual risk, not demonstrated remote exploitability.
**P1:** resolve before security-sensitive use. **P2:** planned hardening.

| ID / priority | Remaining risk and action |
| --- | --- |
| F-01 · Medium · P1 | V1 lacks forward secrecy/recovery and authenticated group epochs. Obtain independent protocol review; stronger claims require a reviewed ratchet/group design with interoperability and crash/recovery evidence. Old messages gain no retroactive protection. |
| F-03 · Medium · P1 | Invite URLs/bodies, database/config exports and logging layers can expose admission credentials. Redact the full request path, protect copies, share passwords separately and revoke leaks. Body activation/no-referrer do not erase URL history or logs. |
| F-04 · Low · P2 | Deployment backups/rollback image tags have no host-side pruning. Define retention that preserves active/recovery material, test restore and monitor disk. [Registry cleanup](../scripts/artifact-cleanup.json) does not prune host copies. |
| F-06 · Low · P2 | Dependency ranges and numbered base/action tags can resolve differently later. Immutable deployment digests preserve checked builds; retain resolved inputs and monitoring. Numeric Action tags remain repository policy. |

## Automated advisory monitoring

[CONTRIBUTING](../CONTRIBUTING.md#security-checks) owns scanner policy, schedules,
reports, notifications and activation. Only `smoke`/`container` are verified
promotion gates. CodeQL and daily scans are asynchronous; successful CodeQL uploads
do not enforce an alert threshold. Read-only verification confirmed Secret Scanning
and Push Protection enabled after the maintainer's settings change. CodeQL default
setup was unconfigured; the coding agent changed no settings.
A minimal advanced CodeQL workflow analyzes Python/JavaScript; do not
also enable default setup. Main's active ruleset requires smoke/container checks.

Pip-audit scans installed distributions, not their vendored libraries. Pip 26.2.2
bundles vulnerable msgpack 1.1.2, setuptools 70.3.0 and urllib3 2.7.0 (six findings,
four High); standalone package upgrades do not replace those copies. The revised
Dockerfile checks binary-wheel dependencies before removing pip/ensurepip, avoids
curl and applies Alpine updates. The image includes the documented admin password-reset
command. Final-image checks verify its presence, runtime imports and tool removal;
image inventory uses stdlib metadata. Alpine uses musl rather than glibc; the
verification below covers the replacement runtime.

## Verification record and gaps

Python 3.12.15/Alpine 3.24.3 images were built and tested for Linux amd64
(emulated) and arm64 (native). Trivy 0.76.1 scanned both exact image IDs, including
OS and Python packages and unfixed advisories: zero findings at all severities.
Strict pip-audit of the amd64 image's 33 installed distributions also passed.
Both builds passed dependency checks and the existing image guards. Live checks
on both architectures verified health, UID 1000, a read-only root filesystem,
admin password reset and persistence after container replacement.

The Alpine amd64 test image preserved every production dependency version. All
481 Python cases passed across a full run and a targeted rerun after fixing an
atomic port-file publication race in the transport fixture. Existing evidence
for the unchanged JavaScript (195 cases) and workflows remains valid. Ruff,
compilation and documentation checks passed; Trivy's Dockerfile configuration
scan reported zero findings.

The previous Debian base finding is resolved in these local images: systemd,
Perl and libacl are absent; libuuid 2.42.3-r1 includes the
[Alpine util-linux fixes](https://secdb.alpinelinux.org/v3.24/main.json), and
ncurses 6.6 is newer than the
[affected ncurses releases](https://security-tracker.debian.org/tracker/CVE-2025-69720).
No advisory exclusions were added. Hosted CI for `2644957` correctly blocked
promotion/release on the old Debian findings while smoke and CodeQL passed.
The replacement has not yet run in hosted CI or been deployed. Scan results
reflect the advisory database at verification time; distribution-only audits
do not cover vendored libraries.

Coverage includes revoked sockets on server pushes, token-role separation,
mixed-owner ACK batches, crypto failure without deletion, and the documented
container recovery command.

Still unverified: browser/device storage behavior, native interoperability,
production proxy/IAM/secrets/logs/resource isolation, restore of an actual operator
backup, bundled-action advisories and an independent cryptographic/security
assessment. Node/OpenSSL and loopback HTTP/WS tests do not establish these properties.

Report vulnerabilities through [SECURITY.md](../SECURITY.md). Keep current
findings/evidence here and release history in [CHANGELOG.md](../CHANGELOG.md).
