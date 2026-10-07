# Security review

Current code: 0.6.0. P-256 replacement and browser key persistence reviewed 2026-10-07; activation and group sender labels reviewed 2026-10-06; broader review 2026-09-27.
This is a source review with regression tests, not an independent audit.
Release history belongs in [CHANGELOG.md](../CHANGELOG.md).

## Implemented protections

| Area | Implemented behavior |
| --- | --- |
| Invite admission | Body-only `POST /api/v1/links/activate` keeps the invite secret in the JSON body; the bundled client uses it. Activation requires HTTPS or local loopback, independently of the global TLS toggle. Invalid supplied bearer authentication returns 401 without anonymous admission; malformed resume/session credentials are rejected before the SQLite write reservation. Slot allocation and password throttling remain transactional. |
| Delivery | Exact acknowledgements cannot delete a newer delivery after SQLite row-ID reuse. Read deletion and receipt creation commit together. Identical uploads reuse the original result within a bounded retry window. |
| Administration | Signed, cookie-bound form tokens prevent cross-site request forgery (CSRF). Login limits persist across processes/restarts. Logout and password resets revoke registered sessions; concurrent old-password logins cannot escape a reset. |
| Client sessions | Short access tokens, hashed refresh credentials, rotation/replay detection, per-device revocation and explicit legacy cutoff controls. Issuance rechecks user/invite validity under the database write reservation. |
| Transport and capacity | HTTPS/WSS outside loopback, private administration by default, bounded HTTP/WS traffic and database queues. Shared runner/Docker suppress raw access and WS INFO logs. |
| Device state | Non-exportable private keys, locally calculated fingerprints, atomic first-use peer pins, encrypted history/outbox and persistence before deletion acknowledgements. A separate committed identity read precedes invite admission; unreadable saved identities are preserved and cannot be overwritten by routine/activation writes. |
| P-256 boundary | Native non-exportable ECDH identity/ephemeral keys; validated uncompressed 65-byte P-256 public points. Retired keys cannot authenticate, issue/refresh sessions, or share a room with new clients. Configuration imports reject invalid keys before replacement/deletion. The new browser database is separate from prior test data, with no old-format decryption or key conversion. |
| Client errors | Fixed local/protocol explanations and allowlisted server-detail translations distinguish storage, identity, login, invite and HTTP failures. Raw server validation inputs, status text, response bodies and unknown exception messages are not displayed. No diagnostic upload, reporting controls or new telemetry is added. |
| Group sender labels | Incoming messages use the matching participant's display name and public ID, saved in encrypted history and rendered with `textContent`. Existing history can resolve current roster names by its saved routing identity without changing delivery IDs or acknowledging old messages again. Names are server-provided labels, not authenticated identities; unnamed senders fall back to public IDs. |
| Recovery | Isolated restore tests verify that both signing-key rotation and removal of restored session records are needed to invalidate old credentials. |

Implementation does not prove that deployment settings are correct or that the
system is secure against every attack.

## Threat model and limits

Assume network observers, leaked databases/backups, stolen bearer credentials,
a malicious relay/key directory, malicious group members, and compromised devices
or browser JavaScript.

- **P-256 encryption v1 has no recipient forward secrecy.** A stolen recipient identity
  key can decrypt recorded inbound traffic: both DH values are recoverable from
  that key and public headers. A stolen sender identity key alone does not recover
  past outbound ephemeral DH values. There is no ratchet or post-compromise
  recovery to restore future secrecy after a compromise ends.
- **First-use pinning is not authentication.** It detects later changes under the
  same peer public ID, not initial substitution, new IDs, or malicious rosters.
  Compare full fingerprints with the owner over a separate trusted channel.
  Investigate mismatches; do not clear history or replace pins to silence them.
- **The browser trusts its origin and device.** Non-exportable keys block normal
  export APIs; same-origin malicious code can still invoke keys and read plaintext.
  CSP cannot protect against an origin changing its own code or policy. An
  independently distributed native client can reduce this code-delivery threat.
- **Bearer credentials do not prove possession of message keys.** Stolen tokens
  cannot alone decrypt messages but can fetch/delete ciphertext or disrupt delivery.
  Room passwords control admission; the server and TLS terminator see them.
- **Remaining invite credential exposure.** `/l/{token}` and
  `/client?invite={token}` contain admission secrets that can enter
  server/proxy/monitoring logs. Opening invite links can also leave browser
  history. Activation carries the secret in its body, which logging tools can
  also record. HTTPS,
  POST, no-store and no-referrer do not eliminate these copies. Suppress/redact
  URLs and request bodies at every logging layer; an open, unprotected leaked
  invite permits a new join.
- **The relay sees metadata.** Identities, membership, timing, sizes and receipts
  are visible. Receipts are server assertions, not cryptographic proof of reading.
  The relay can delay, suppress, reorder, replay or fabricate metadata.

The exact encryption format and client obligations are in [PROTOCOL.md](PROTOCOL.md).
HTTP/WS request fields, limits and retry rules are in [API.md](API.md).

## Authentication boundaries

HTTP and WebSocket authorization check the user/public identity, issuing invite,
expiry, explicit revocation and deletion. A full/consumed invite is not revoked.
WebSockets revalidate before delivery/backlog, on incoming frames and every
30 seconds while idle. Checks cannot retract a response already authorized or
remove the small check/send race.

Renewable access defaults to 15 minutes; sessions have an absolute 30-day limit,
capped by invite expiry. Refresh never extends the stored lifetime. Old refresh
digests remain until session expiry for replay detection. An identical old/new
pair may retry for 30 seconds while the successor remains current; other reuse
revokes the session. Clients persist the proposed successor before sending it.
Browser Web Locks coordinate rotations and outgoing retries across tabs.

Admission may commit a participant slot before session issuance. Issuance
rechecks the current user/invite and, for legacy migration, token expiry and the
configured cutoff. Rejection does not roll back admission; keep resume credentials.

Compatibility and recovery boundaries:

- Legacy JWTs remain usable until expiry or `SIDEWORD_LEGACY_TOKEN_DEADLINE`.
  They bypass per-device session revocation until then.
- Invite resume credentials intentionally survive session revocation and allow
  recovery into a fresh session. Revoke/delete the invite if those secrets leak.

Admin cookies are HttpOnly/SameSite=Strict and Secure on HTTPS or a configured
HTTPS public URL. Unsafe cookie requests require CSRF and origin checks;
foreign origins are rejected. Missing/null Origin requires same-origin Fetch
Metadata plus CSRF, or the supported explicit-header flow described in the API.
Cookie-free API bearer requests still require a live admin session. Persistent
login limits use keyed hashes of account/IP identifiers; attackers can still
cause temporary lockouts.

## Delivery, local storage and retention

Authenticate/decrypt and persist local history before ACK/read deletion.
Storage or key-check failures keep deliveries retryable. Exact references bind
a random delivery ID, chat, sender/reader and client message ID. Deduplicate by
logical identities, not SQLite row IDs; treat naive timestamps as UTC.

The send ledger retains hashes, routing metadata and timestamps, not message
plaintext or ciphertext. It survives read/ACK/outbox deletion for the configured
retry window (default 30 days). Conflicting retries return 409. Pre-upgrade queued
rows without ledger evidence reject matching IDs; already deleted older messages
and evidence missing from backups have no retry guarantee. Do not automatically
resend beyond a saved retry window. Legacy ACK/read matching remains ambiguous
until disabled with `SIDEWORD_ALLOW_LEGACY_ACK=false`.

Queued ciphertext and receipts expire after the configured TTL (default 30 days)
or are consumed by read/ACK. Cleanup runs at startup and hourly. Users, memberships,
invite credentials/verifiers, names, last-seen times and exports persist separately.
SQLite deletion is not secure erasure: WAL, free pages, snapshots and backups may
retain bytes. Apply separate backup/export retention and access controls.

Browser history uses a separate non-exportable AES key with the storage location
as authenticated data. Clearing site data, changing origin or Reset device can
permanently lose keys/history. A future ratchet does not protect retained plaintext
on a compromised device. CSP blocks inline/third-party scripts, framing and
cross-origin connections; content uses `textContent`. Non-static responses use
no-store/no-referrer headers.

Opening a different invite pauses the old chat in that tab until explicit
activation; other tabs continue. Keys, encrypted history and pending sends remain.
Records are scoped by participant public ID for later authorized reconnects.
The last activated invite/room is stored with the session, and routine identity
writes preserve newer session/invite state from another tab. Switching invites
is not a privacy wipe; Reset device explicitly clears local storage.

Backend 0.6.0 intentionally replaces the test encryption format without backwards
support. The browser uses `sideword-test-client-p256`; prior device data is neither
read nor automatically deleted. Reset device clears only the new database.
Old server records are not converted or erased, and old credentials are rejected
independently of JWT/refresh expiry. Create new rooms/invites and reverify new
fingerprints; do not treat a new participant public ID as a verified old identity.

The browser checks the saved identity after committing admission credentials and
before sending activation. An absent IndexedDB record permits new setup; a null
deserialization result or null saved key is treated as unreadable storage. Reads
wait for transaction completion, and failed reads do not release session state.
Unreadable records cannot be replaced by a new identity; startup failures remain
visible on the join form. Renewal distinguishes missing/unreadable storage from
an actual changed participant/public key and does not renew either case.

P-256 removes the dependency on the key type affected by the reported
[WebKit persistence bug](https://bugs.webkit.org/show_bug.cgi?id=312279).
The storage preflight remains and rejects failed round trips before admission;
it does not repair browser storage or recover inaccessible keys. The reported
user-device failure has not been directly reproduced in a browser. Node adapters
verify the null-read failure path and native P-256 key cloning/usage, not the
underlying WebKit implementation. There is no exportable-key fallback. Actual
Safari/iOS persistence and chat behavior remain a release-QA requirement.

## Deployment and recovery obligations

Use the [upgrade/recovery guide](UPGRADING.md) for the procedure. In particular:

- Keep admin/schema routes private for both HTTP and WebSocket upgrades. Only
  trust known proxy IPs; configure public TLS, HSTS and credential-log suppression.
- Supplement per-process HTTP/WS limits with gateway and host resource limits.
  Database quotas/login counters are shared. Admission password verification holds
  a SQLite write reservation; admin password hashing runs off the event loop.
  Rate limits bound cost but do not guarantee availability against distributed
  traffic, slow peers or exhausted unauthenticated quotas.
- Restoring a backup also restores authentication state. Rotate the signing
  secret, remove restored refresh/client/admin sessions, and reapply later invite
  revocations, user deactivations and password resets before reopening access.
  Recovery cannot reconstruct missing send evidence or subsequent policy changes.

## Remaining work and closure criteria

| Work | Completion evidence needed |
| --- | --- |
| Retire legacy session/delivery modes | Verify every supported client uses renewable sessions/exact ACKs, then configure `SIDEWORD_LEGACY_TOKEN_DEADLINE` and `SIDEWORD_ALLOW_LEGACY_ACK=false`. Deployment of these controls has not been verified here. |
| Deployment hardening | Verify actual proxy trust, TLS/HSTS, private routes, logging, backup protection and resource limits for every intended deployment. |
| Operator recovery drill | Restore an isolated copy of the operator's backup and verify credential invalidation, authorized reconnects and retained delivery state. Automated fixtures do not verify infrastructure or historical server/client rollback. |
| Broader verification | Real-browser QA and independent security review; separately track dependency/advisory review, penetration testing and load testing. Browser automation was excluded from this work. |
| Stronger encryption | A separately reviewed protocol migration for forward secrecy and post-compromise recovery; requirements below. |

Close a specific issue when its agreed fixes and acceptance checks pass. Keep
the remaining tasks separately tracked; issue closure is not security certification.

For a future protocol, evaluate maintained implementations of
[Signal's Double Ratchet](https://signal.org/docs/specifications/doubleratchet/)
with authenticated asynchronous setup, and [MLS](https://www.rfc-editor.org/rfc/rfc9420.html)
for groups. [libsignal](https://github.com/signalapp/libsignal) and
[OpenMLS](https://github.com/openmls/openmls) are candidates, not dependencies.
Recheck releases, advisories, audit scope, licensing and browser/native support
before selection. Switching primitives alone does not add a ratchet; sealed
boxes also omit sender authentication.

Require authenticated version negotiation and identity transitions, no silent
downgrade, crash-consistent key/history/ACK state, persistent outgoing ciphertext,
bounded skipped keys and cross-language test vectors. Group upgrades need member
agreement, authenticated membership epochs and defined leaving-member access.
For a future production upgrade, define preservation of the current P-256 receive
format and local history during an explicit transition. Dual-encrypting new
messages with this format does not provide ratchet security. This requirement
does not add support for the retired pre-0.6.0 test format.
Review upgrade/rollback before rollout: never reset ratchet state or reuse keys.
Old messages gain no retroactive forward secrecy.

## Latest verification record

- **0.6.0 code, 2026-10-07:** Ruff, dependency consistency, Python compilation,
  client/protocol/form syntax, release notes and whitespace checks passed.
  Python regression coverage totals 189 passing cases across the full run and
  affected-test rerun; JavaScript coverage totals 76 passing cases across the full
  run and affected-test reruns. Coverage includes native point
  validation, independent OpenSSL encryption/decryption, non-exportable keys,
  malformed and retired-key rejection, blocked mixed rooms, failed-storage
  admission and import rejection before replace-mode mutation. No browser
  verification was performed; Docker was unavailable for a local container build.
  Clearer-error verification passed client/protocol syntax, 64 affected JavaScript
  cases (with 18 retry cases rerun after an outdated wording assertion), the Python
  smoke test, release-note generation and whitespace checks. Error regressions
  verify HTTP explanations, private validation-value filtering, unknown exceptions,
  storage/encryption failures and retained refresh proposals after a lost response.
- **Release automation:** version/notes validation, workflow configuration,
  Bash syntax and nine mocked publication scenarios passed locally, including
  outdated commits, existing releases, conflicting tags and command failures.
  The workflow has not yet run on GitHub; repository permissions and successful
  remote publication remain unverified.
- **Coverage:** crypto interoperability/tampering, key pinning, persistence failures,
  exact delivery and retry races, CSRF/admin revocation, client refresh/issuance
  boundaries, isolated restore scenarios, transport limits and invite navigation.
  Activation regressions cover body-token validation privacy, invalid bearer
  rejection without slot allocation, HTTP rejection despite the disabled global
  TLS guard, loopback/spoofed-header boundaries, and body-route retries sharing
  identity, sessions and capacity. Removed path routes return 404 without
  admission or resumption and are absent from OpenAPI. Node checks verify that activation retries
  keep the token in JSON and preserve saved credentials after a rejected bearer.
  Group-label regressions cover polling/live delivery, matching chat rosters,
  duplicate names, unnamed fallbacks, text-only rendering, persistence before
  acknowledgement, and encrypted history reload without a roster.
  Follow-up regressions cover the rendered script URL, peer-name preservation,
  existing history relabeling without node replacement or acknowledgement, and
  malformed/other-chat history fallback.
  History-scroll regressions cover hidden-panel rendering, encrypted startup
  history with unavailable synchronization, and preserving an older reading position.
  Storage regressions cover committed reads, transaction aborts, non-exportable
  key round trips, WebKit-style null reads, partial null keys, preserving unreadable
  records, missing versus changed devices, blocked admission before network activity,
  retained admission proposals, successful retry after storage recovery, and inline startup errors.
- **Deployment:** read-only inspection on 2026-10-07 verified a healthy cloud
  container reporting 0.5.0, zero container restarts and no OOM kill, and public
  JavaScript matching the 0.5.0 repository source. Retained deployment logs and
  service journals showed no matching application exceptions. Application logging
  is disabled; nginx access logging is off and errors are discarded, so logs
  cannot establish the user-device failure's cause. The cloud backend runs
  independently of the local admin tunnel. The 0.6.0 change has not been deployed.
- **Limits:** tests use isolated databases and Node adapters for browser storage,
  not a live browser. They do not establish native interoperability, production
  configuration, penetration/load-test results, a full dependency audit or a
  cryptographic proof.

For future updates, replace the current status and latest verification record
when new evidence exists. Keep unresolved boundaries and acceptance criteria;
put release history in the changelog instead of appending development diaries.
