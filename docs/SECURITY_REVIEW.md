# Security review

**Reviewed:** 2026-10-09 · **Source baseline:** working tree based on `bbeab25` · **Backend:** 0.11.1

Sideword is a ciphertext relay with a bundled browser **test client**. Source and
regression coverage support the controls below, but the client protocol has no
recipient forward secrecy or post-compromise recovery. This maintainer review is
not an independent audit, cryptographic proof, or certification of a deployment.

## Scope and assessment basis

This review covers authentication, invite admission, HTTP/WebSocket authorization,
client encryption and storage, relay retention, administration, container defaults,
and repository CI/deployment configuration. It inspects implementation and existing
tests; it does not assess private runtime configuration or cloud account settings.

- **Verified:** implementation inspected and relevant regression coverage identified;
  this does not imply that every execution path or deployment is verified.
- **Partial:** a control exists but depends on configuration or has a stated limit.
- **Known weakness / design limit:** behavior is established by implementation,
  rather than inferred from a missing test.
- **Unverified:** evidence is insufficient; no vulnerability is asserted.

The structure follows [OWASP assessment reporting](https://wstg.owasp.org/v4.2/5-Reporting/),
with qualitative likelihood and impact assessment informed by
[NIST SP 800-30](https://csrc.nist.gov/pubs/sp/800/30/r1/final). This is not a claim
of compliance with either framework.

## Architecture and trust boundaries

| Boundary | Security responsibility |
| --- | --- |
| Browser/device → relay | The client encrypts message content and checks peer keys. The relay authorizes routing and sees public identities, membership, timing, sizes, and confirmations. |
| Invite holder → participant | Invite and optional room-password possession authorize admission. The server and TLS terminator receive these credentials; they are not message-encryption keys. |
| Administrator → server | Administration controls accounts, invites, conversations, and configuration exports. Restrict access separately from public client traffic. |
| Application → persistent storage | SQLite holds ciphertext, metadata, invite secrets, and authentication state. Database files, exports, and backups need operator access and retention controls. |
| Build/deployment → runtime | Trusted repository changes and CI produce the selected image. Privileged deployment commands activate it; private runtime configuration stays outside the repository. |

The relay cannot establish a person's identity from a public ID or display name.
It accepts opaque payloads and cannot verify that third-party clients encrypt correctly.
Sessions authorize the participant across retained memberships; revoking one invite
does not remove those memberships or invalidate other legitimate sessions. Explicit
conversation closure blocks live messaging while preserving authorized archive
access. See [session lifecycle](SESSION_LIFECYCLE.md) for the policy and edge cases.

## Controls and supporting evidence

| Area / status | Implemented protection and boundary | Evidence |
| --- | --- | --- |
| Invite admission — **Verified** | Body-based activation requires HTTPS or loopback even when the general HTTPS guard is disabled. Malformed credentials and invalid supplied authentication fail before admission. Password checking, throttling, and slot allocation use a SQLite write reservation. | [Admission](../app/routers/links.py), [password controls](../app/invite_security.py), [invite regressions](../tests/test_invite_protection.py). |
| Client authentication — **Verified** | Renewable sessions use short access JWTs, hashed refresh credentials, rotation/replay detection, and per-device revocation. User, issuing invite, and session validity are rechecked; refresh cannot extend the absolute session lifetime. JWTs without a registered session are rejected; legacy issuance/migration and ambiguous ACK/read routes are removed. | [Authorization](../app/deps.py), [sessions](../app/routers/sessions.py), [issuance boundaries](../tests/test_client_session_boundaries.py), [session regressions](../tests/test_security_stages.py). |
| Conversation authorization — **Verified** | Membership gates HTTP delivery and status queries. WebSockets revalidate during traffic and periodically while idle. Conversation closure applies across sessions; late ownership-checked durable ACKs remain possible. Presence is membership-scoped but requires one shared process. | [Routing checks](../app/services.py), [WebSockets](../app/routers/ws.py), [closure tests](../tests/test_conversation_closure.py), [presence tests](../tests/test_presence.py). |
| Administration — **Verified** | Registered admin sessions, bcrypt password hashing, persistent login throttles, cookie-bound CSRF and origin checks. Cookies are HttpOnly/SameSite=Strict and Secure on HTTPS or a configured HTTPS public URL. Logout/password resets revoke sessions; password-reset races are covered. Password length has a known limit (F-05). | [Admin authentication](../app/routers/admin_auth.py), [guards](../app/guards.py), [admin race tests](../tests/test_admin_login_races.py), [CSRF/revocation tests](../tests/test_security_stages.py). |
| Message encryption — **Partial** | The browser uses P-256 ECDH, HKDF-SHA-256, and AES-256-GCM with conversation/sender/recipient/message context binding. Public points are validated. Interoperability and tamper tests support implementation behavior, not protocol security certification. | [Protocol code](../app/static/client-protocol.js), [point validation](../app/message_keys.py), [crypto tests](../tests/client_crypto.test.cjs), [backend key tests](../tests/test_p256.py). |
| Device storage and delivery — **Verified** | Non-exportable origin-local identity/storage keys, atomic first-use pins, encrypted local history/outbox, and persistence before deletion ACKs. Shared rooms do not merge identities/history. Exact delivery references resist SQLite row-ID reuse; retries are bounded and transactional. Device credentials in the identity record are not covered by history encryption. | [Client](../app/static/client.js), [identity tests](../tests/client_identity.test.cjs), [delivery tests](../tests/client_delivery.test.cjs), [alias routing/authorization tests](../tests/test_cross_origin_delivery.py), [exact-ACK tests](../tests/test_exact_delivery.py). |
| Transport and public access — **Partial** | HTTPS/WSS is required outside loopback by default. Aliases need one backend process/database for shared live delivery; the connection registry is process-local. The shared runner's public listener rejects admin/schema HTTP and WebSocket routes. Docker's default listener includes admin routes; isolation and trusted-proxy configuration remain essential. | [Transport guards](../app/guards.py), [connection registry](../app/ws_manager.py), [shared runner](../scripts/serve_shared.py), [listener tests](../tests/test_shared_listener.py), [Dockerfile](../Dockerfile). |
| Responses and logging — **Partial** | Client CSP excludes inline/third-party scripts; user content uses text-only rendering. Non-static responses use no-store/no-referrer. Validation responses omit supplied input values. Shared-runner/container access logs are suppressed; deployment helpers allowlist output. Other logging layers remain outside these guarantees. | [Client headers](../app/routers/client.py), [response handling](../app/main.py), [static tests](../tests/test_static_delivery.py), [deployment-output tests](../tests/test_deploy.py). |
| Availability — **Partial** | Request/body/frame/connection limits and queue quotas bound resource use. Client recovery bounds HTTP/body and socket waits, uses capped randomized retries, and releases disconnected sockets/timers; matching device identity and initialization precede live status. HTTP/WS limits are process-local; password verification and SQLite writes consume finite resources. No distributed denial-of-service resistance is established. | [Limits](../app/guards.py), [queues](../app/routers/chats.py), [capacity tests](../tests/test_security_stages.py), [client recovery tests](../tests/client_connection.test.cjs). |
| Build and deployment — **Partial** | CI gates exact-commit promotion; manual deployment uses OIDC, the retained CI image, and an immutable registry digest bound to its image configuration. Deployment locks, backups, health/private-route checks, and restricted failure output are implemented. Runtime credentials/configuration are not copied into CI. The image runs as a non-root user. Live IAM/network restrictions require separate verification. | [CI](../.github/workflows/test.yml), [deployment workflow](../.github/workflows/deploy.yml), [deployment helper](../scripts/deploy.py), [deployment tests](../tests/test_deploy.py), [Dockerfile](../Dockerfile). |

## Threat model and limits

Consider network observers, malicious participants or relay operators, stolen
bearer credentials, leaked storage/backups, and compromised origins or devices.

- **Key compromise:** the v1 protocol has no recipient forward secrecy. A stolen
  recipient identity key exposes recorded inbound ciphertext; there is no ratchet
  to recover future secrecy after compromise. See F-01.
- **Peer identity:** first-use pinning detects later changes under the same public
  ID; it cannot authenticate initial keys, replacement IDs, or group membership.
  Compare full fingerprints through a separate trusted channel. Names are labels.
- **Origin/device trust:** non-exportable keys prevent normal key export, but
  malicious same-origin code can invoke them and read plaintext. CSP does not
  protect against the origin replacing its own application. Device compromise
  also exposes retained local history and credentials.
- **Bearer authority:** tokens do not prove possession of message keys. Token
  theft alone does not decrypt messages, but can disrupt delivery or delete
  ciphertext. Resume credentials intentionally survive session revocation;
  compromised admission credentials require invite revocation or user deactivation.
- **Relay assertions:** metadata and confirmations are not cryptographic proof of
  identity, delivery, or human attention. The relay can manipulate availability
  and metadata. Revocation or closure cannot retract plaintext already received.

The [protocol contract](PROTOCOL.md) contains the wire format and client obligations;
the [API contract](API.md) defines authorization, delivery, and presence semantics.

## Delivery, local storage and retention

Queued ciphertext and receipts are deleted after ACK/read or the configured TTL
(default 30 days); cleanup runs at startup and hourly. Retry/confirmation metadata
outlives ciphertext until both retry and retention deadlines permit removal.
Viewing does not delay ciphertext deletion. Users, memberships, invite secrets,
and configuration exports have separate lifecycles. Evidence:
[cleanup](../app/cleanup.py), [stored data](../app/models.py),
[message-status tests](../tests/test_message_status.py).

SQLite deletion is not secure erasure of WAL/free pages, snapshots, or backups.
The application does not encrypt database files or exported configuration;
exported invite secrets remain sensitive. Clearing browser site data or resetting
the device can permanently lose keys/history. Changing origins does not transfer
them; access to the original origin's storage is needed for saved history. The retired
pre-P-256 format is not read or converted; see [upgrade/recovery guidance](UPGRADING.md).

## Prioritized findings and improvements

Ratings describe residual risk under the stated trust boundaries, not demonstrated
production exploits. **Medium** denotes material confidentiality, integrity, or
availability impact with the prerequisite stated below; **Low** denotes a narrower
effect. **P1** should be resolved or explicitly accepted before security-sensitive
use; **P2** is planned hardening. Verification gaps are listed separately.

| ID / risk / priority | Established finding and impact | Existing mitigation and next step |
| --- | --- | --- |
| **F-01 · Medium · P1** | **Design limit:** recipient-key compromise exposes recorded inbound messages, and the protocol has no post-compromise recovery. [Protocol implementation](../app/static/client-protocol.js). | Non-exportable keys reduce normal export exposure. Before stronger privacy claims, independently review a maintained ratcheting protocol with authenticated identity/group transitions. Require explicit migration, downgrade protection, interoperability and crash/recovery evidence. Old messages gain no retroactive forward secrecy; origin/device trust remains a separate boundary. |
| **F-03 · Medium · P1** | **Known credential exposure paths:** invite links and legacy WebSocket authentication can place secrets in URLs/headers; request-body logging can capture activation credentials. Invite tokens are also stored in database/configuration exports. Leaked usable credentials can permit admission or disrupt delivery. [Invite navigation](../app/routers/client.py), [WebSocket authentication](../app/routers/ws.py), [exports](../app/routers/admin_export.py). | Body-based activation, first-frame WebSocket authentication, no-store/no-referrer, optional passwords, and suppressed access logs reduce exposure. Verify redaction across the entire request path and protect database/export/backup access. Plan a compatible invite handoff that reduces URL copies; revoke leaked credentials. |
| **F-04 · Medium · P2** | **Known host-retention gap:** deployment creates database backups and rollback image tags without automatic pruning. Repeated deployments can retain sensitive state and eventually exhaust storage. [Deployment helper](../scripts/deploy.py). | Backups have private permissions. Registry cleanup has [a separate policy](../scripts/artifact-cleanup.json) and does not prune host copies. Define and verify host retention that preserves the active image and an agreed recovery backup/image; test cleanup and restore before deleting recovery material. |
| **F-05 · Low · P2** | **Known password-input limitation:** admin password hashing and verification silently truncate UTF-8 input to 72 bytes; creation/bootstrap do not reject overlength input. [Password helpers](../app/security.py), [admin creation](../scripts/create_admin.py), [bootstrap](../app/admin_setup.py). | Private administration and login throttling limit exposure. Reject unsupported new inputs with a clear byte-length rule, or plan a compatible hashing migration. Verify creation, login, and reset boundaries without unexpectedly invalidating existing accounts. |
| **F-06 · Low · P2** | **Partial supply-chain reproducibility:** dependency ranges, the base-image tag, and action version tags can resolve to different inputs on later builds. This is not evidence of a vulnerable dependency. [Requirements](../requirements.txt), [Dockerfile](../Dockerfile), [CI](../.github/workflows/test.yml). | Immutable deployment digests preserve the already-checked build. [Dependabot](../.github/dependabot.yml) schedules pip/action updates. Record resolved build inputs and establish recurring advisory review; dependency compatibility checks alone are not vulnerability scans. |

## Verification record and gaps

Verification on 2026-10-09 covers source/configuration inspection, repository
references, Python regressions, Node client regressions, lint, and compilation.
Session regressions reject unregistered JWTs across HTTP/WebSocket/admission,
require activation credentials before database writes, exercise revocation/expiry
at issuance, and confirm removed deletion routes preserve queued messages.
Browser adapter tests cover retired-login handling without key/history deletion.
Alias regressions cover same-host and mixed-host bidirectional delivery, distinct
device identities, refresh/reconnect backlog, immutable retry IDs, recipient ACKs,
unrelated-participant isolation and expired-session rejection. Client regressions
cover origin-relative endpoints, identity mismatch, stalled requests/connections,
backoff, offline/navigation recovery, and obsolete socket events. Native crypto
tests exchange envelopes between independent origin-specific devices.
Suites use isolated databases and Node adapters, not a live browser. They do not
establish authenticated end-to-end delivery through an operator's complete proxy
chain or verify third-party client migration. No production configuration or
deployment was changed for this release.

| Unverified area | Evidence needed to establish the property |
| --- | --- |
| Runtime isolation and configuration | Per-installation review of proxy trust, TLS/HSTS, public HTTP/WS route blocking, log redaction, IAM/federation restrictions, secret handling, storage permissions, and resource limits. Source configuration alone cannot establish these. |
| Recovery | An isolated operator-backup restore with signing-key rotation, removal of restored sessions/refresh state, and reapplication of later revocations/resets. [Restore tests](../tests/test_session_recovery.py) cover fixtures, not the operator's backups or historical rollback. |
| Browser/native interoperability | Real-device key persistence, recovery after interrupted storage/refresh, and message exchange. Node/OpenSSL checks do not verify Safari/iOS storage or native-client interoperability. |
| Dependency and independent assessment | Current resolved Python/OS/container advisory review, confirmation of repository security settings, and appropriately scoped independent cryptographic/security assessment. No current comprehensive advisory scan or penetration/load assessment is established here. |

## Reporting and upkeep

Use [SECURITY.md](../SECURITY.md) for supported versions and private vulnerability
reporting. Confirmed vulnerabilities requiring disclosure belong in coordinated
[GitHub security advisories](https://docs.github.com/en/code-security/concepts/vulnerability-reporting-and-management/repository-security-advisories);
this public review must not contain secrets, private deployment details, or exploit
instructions.

Update the baseline, affected evidence, and current finding status together.
Close a finding only with verified remediation or a documented, scoped risk
acceptance; retain unresolved limits and replace superseded evidence. Follow the
risk-based vulnerability response principles in
[NIST SSDF](https://csrc.nist.gov/pubs/sp/800/218/final). Keep release history in
[CHANGELOG.md](../CHANGELOG.md), rather than appending audit diaries here.
