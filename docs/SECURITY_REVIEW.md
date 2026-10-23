# Security review

**Reviewed:** 2026-10-22 · **Backend:** 0.13.1

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
| F-07 · High · P1 | The last hosted image scan found 290 findings, including 52 High. Remove runtime installer/curl exposure and verify the rebuilt image. The base previously had 44 High findings across eight unfixed advisories; promotion must remain blocked pending upstream fixes or a verified base/runtime replacement. |

F-07's unresolved base advisory IDs: CVE-2025-69720 (ncurses), CVE-2026-16742
(systemd/libudev), CVE-2026-54369 (libacl), CVE-2026-76642/CVE-2026-78408/
CVE-2026-78409/CVE-2026-78410 (util-linux), CVE-2026-9538 (Perl).
Debian still lists [systemd](https://security-tracker.debian.org/tracker/CVE-2026-16742)
and [Perl](https://security-tracker.debian.org/tracker/CVE-2026-9538) as vulnerable
in trixie. Package presence is not proof that an affected application path is reachable.

## Automated advisory monitoring

[CONTRIBUTING](../CONTRIBUTING.md#security-checks) owns scanner policy, schedules,
reports, notifications and activation. Only `smoke`/`container` are verified
promotion gates. CodeQL and daily scans are asynchronous; successful CodeQL uploads
do not enforce an alert threshold. Read-only verification confirmed Secret Scanning
and Push Protection enabled after the maintainer's settings change. CodeQL default
setup was unconfigured; the coding agent changed no settings.
A minimal advanced CodeQL workflow is prepared for Python/JavaScript; do not
also enable default setup. Main's active ruleset requires smoke/container checks.

Pip-audit scans installed distributions, not their vendored libraries. Pip 26.2.2
bundles vulnerable msgpack 1.1.2, setuptools 70.3.0 and urllib3 2.7.0 (six findings,
four High); standalone package upgrades do not replace those copies. The revised
Dockerfile checks dependencies before removing pip/ensurepip, avoids curl and
applies Debian updates. The image includes the documented admin password-reset
command. Final-image checks verify its presence, runtime imports and tool removal;
image inventory uses stdlib metadata. This does not fix the remaining base findings.

## Verification record and gaps

Final validation used Python 3.12.12 and isolated databases: all 481 Python cases
and 195 Node cases passed across full runs and targeted reruns. Ruff, compilation,
actionlint and documentation links passed; Trivy's Dockerfile configuration scan
reported zero findings. Existing pip check/resolution evidence remains valid:
Python requirements did not change. Real pip-audit previously accepted an exact
distribution inventory and blocked a vulnerable pin; Trivy/report replay blocked
High findings and sanitized reports. An isolated real pip uninstall removed its
vulnerable vendored files.

Hosted CI for `238f0a7` built/verified the image, passed smoke, then blocked 52 High
findings and skipped promotion/release. The revised image has **not** been built
or scanned locally: Docker is unavailable. The prior zero-advisory distribution
audit did not establish a clean image, because it omitted pip's vendored libraries.

Coverage includes revoked sockets on server pushes, token-role separation,
mixed-owner ACK batches, crypto failure without deletion, and the documented
container recovery command. No GitHub-hosted CodeQL run is established here.

Still unverified: browser/device storage behavior, native interoperability,
production proxy/IAM/secrets/logs/resource isolation, restore of an actual operator
backup, bundled-action advisories and an independent cryptographic/security
assessment. Node/OpenSSL and loopback HTTP/WS tests do not establish these properties.

Report vulnerabilities through [SECURITY.md](../SECURITY.md). Keep current
findings/evidence here and release history in [CHANGELOG.md](../CHANGELOG.md).
