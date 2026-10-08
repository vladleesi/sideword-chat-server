# Security review

Current code: 0.9.0. Client UI, sender/reader labels, P-256 replacement and browser key persistence reviewed 2026-10-07; activation and group sender labels reviewed 2026-10-06; broader review 2026-09-27.
This is a source review with regression tests, not an independent audit.
Release history belongs in [CHANGELOG.md](../CHANGELOG.md).

## Implemented protections

| Area | Implemented behavior |
| --- | --- |
| Invite admission | Body-only `POST /api/v1/links/activate` keeps the invite secret in the JSON body; the bundled client uses it. Activation requires HTTPS or local loopback, independently of the global TLS toggle. Invalid supplied bearer authentication returns 401 without anonymous admission; malformed resume/session credentials are rejected before the SQLite write reservation. Slot allocation and password throttling remain transactional. |
| Delivery | Exact acknowledgements cannot delete a newer delivery after SQLite row-ID reuse. Durable delivery and actual viewing are distinct, bound to original recipients and delivery IDs; legacy read deletion and receipt creation still commit together. Identical uploads reuse the original result within a bounded retry window. |
| Administration | Signed, cookie-bound form tokens prevent cross-site request forgery (CSRF). Login limits persist across processes/restarts. Logout and password resets revoke registered sessions; concurrent old-password logins cannot escape a reset. |
| Client sessions | Short access tokens, hashed refresh credentials, rotation/replay detection, per-device revocation and explicit legacy cutoff controls. Issuance rechecks user/invite validity under the database write reservation. |
| Transport and capacity | HTTPS/WSS outside loopback, private administration by default, bounded HTTP/WS traffic and database queues. Shared runner/Docker suppress raw access and WS INFO logs. |
| Device state | Non-exportable private keys, locally calculated fingerprints, atomic first-use peer pins, encrypted history/outbox and persistence before deletion acknowledgements. A separate committed identity read precedes invite admission; unreadable saved identities are preserved and cannot be overwritten by routine/activation writes. |
| P-256 boundary | Native non-exportable ECDH identity/ephemeral keys; validated uncompressed 65-byte P-256 public points. Retired keys cannot authenticate, issue/refresh sessions, or share a room with new clients. Configuration imports reject invalid keys before replacement/deletion. The new browser database is separate from prior test data, with no old-format decryption or key conversion. |
| Client errors | Fixed local/protocol explanations and allowlisted server-detail translations distinguish storage, identity, login, invite and HTTP failures. Raw server validation inputs, status text, response bodies and unknown exception messages are not displayed. No diagnostic upload, reporting controls or new telemetry is added. |
| Participant labels | Names come from the matching chat roster, are saved in encrypted history and rendered with `textContent`. Names are unauthenticated labels; public IDs remain the routing identities. Relabeling history does not change delivery IDs or acknowledge old deliveries. |
| Client UI | Full public IDs and locally calculated fingerprints remain selectable and copyable in participant details. Local pins are explicitly not verified identities; changed-key warnings and blocked sending remain in the active conversation. Pending retries stay accessible across mobile views. Device settings expose public identity and session timing, never private keys or credentials. Appearance storage contains only a theme preference. Mobile navigation uses per-tab history state containing only the device public ID, chat ID and view; restoration checks device scope and roster membership. No keys, credentials or plaintext enter that state. Native modal dialogs contain focus and support Escape; client CSP allows bundled same-origin fonts without inline code or remote resources. Saved-device restoration keeps activation hidden until local reads complete; storage failures reveal the existing inline error. Invite landing pages preserve admission/status guidance and only reveal the existing token on expansion. Admin sign-in keeps its original template and styles. |
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

Optional presence snapshots revalidate recipients and query current conversation
memberships; they expose only public participant IDs and ephemeral connection
status to members. Online means at least one authenticated socket, not an
unexpired session. Opted-in sockets expire after 75 seconds without an incoming
frame; legacy sockets retain transport ping/pong cleanup. Multiple sessions are
validated separately. No new activity records or last-seen timestamps are stored.
The client replaces snapshots immediately outside the encrypted delivery queue,
expires their monotonic leases within 35 seconds, and clears them on connectivity
or authentication loss; absent or expired status is unknown. Presence is a bounded
heartbeat observation, not proof of attention or identity. Independent workers
must disable presence because their local registries cannot establish global
offline status; see the [API contract](API.md#participant-presence).

Presence regression coverage in `tests/test_presence.py` and
`tests/client_ui.test.cjs` covers membership scoping/removal, legacy compatibility,
invalid authentication, consumed invites, separate-session revocation, multiple
connections, renewable-session expiry, disconnect/reconnect, cancelled-socket
cleanup (including cancellation before/after initial presence publication), stale
cleanup, replacement public identities, paused client timers,
unknown status, malformed snapshots and delivery-queue isolation. Verification on
2026-10-07 passed all 210 isolated Python cases (including 24 focused
presence/authentication cases), all 118 JavaScript cases, Ruff, compilation,
client/form syntax, release metadata, changed documentation links and diff
whitespace checks. These checks did not verify a production rollout; browser QA
was excluded.

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

Authenticate/decrypt and persist encrypted local history before durable ACK deletion.
Actual viewing requires the active, focused, visible conversation and message
viewport dwell; background persistence does not count as reading. Confirmations
are authenticated recipient assertions and cannot prove human attention.
[The API contract](API.md#durable-delivery-and-actual-viewing) defines the rule,
original recipient binding, encrypted migration and idempotent recovery.
Storage or key-check failures keep deliveries retryable. Exact references bind
a random delivery ID, chat, sender/reader and client message ID. Deduplicate by
logical identities, not SQLite row IDs; treat naive timestamps as UTC.

The send ledger retains hashes, routing metadata and timestamps, not message
plaintext or ciphertext. It includes original recipient delivery IDs and first
delivery/viewing times, survives ACK/outbox deletion until the later of original
retry expiry and message TTL (each defaults to 30 days), and retains the existing
ledger quota. Ciphertext never waits for viewing. Confirmation queues remain
bounded; overflow commits the ledger state for sender-only status recovery.
Status queries and queued/live receipt exposure require current sender membership. Conflicting retries return 409. Pre-upgrade queued
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
The latest protected invite is saved encrypted in the same tab for up to 24 hours;
closing the tab or clearing browser data can lose it.

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

- **Presence cancellation follow-up, 2026-10-07:** CI exposed cancellation during
  authenticated WebSocket setup, outside the previous receive-loop cleanup guard.
  One shielded finalizer now covers every successfully registered connection,
  including hello/backlog/presence initialization and authentication-session exit.
  It removes only that socket and publishes refreshed, authorized peer snapshots;
  cancellation still propagates after cleanup. Deterministic regressions pause
  setup before and after initial presence publication and verify offline updates,
  registry cleanup and continued observer ping/presence behavior. Local Python
  3.13 validation passed 229 isolated tests and 30 cancellation cases across ten
  independent runs, with Ruff, compilation, whitespace and release-note checks.
  Linux/Python 3.12 CI and production rollout were outside this local verification.

- **Message confirmations, 2026-10-07:** implementation separates durable delivery
  and visibility-based viewing, retains original recipient identities and stores
  immutable receipt metadata encrypted in browser history. Focused regressions
  cover partial groups, identity matching, concurrent acknowledgements, legacy
  semantics, expiry, queue pressure, authorization, WS/poll recovery, encrypted
  reload/tab merges and accessible desktop/mobile message details. Prior full local
  validation passed 226 isolated Python and 150 JavaScript cases, Ruff, compilation,
  client/form syntax, whitespace and release-note checks. New encrypted metadata
  uses hashed event keys so recipient bindings stay inside encrypted values.
  Mixed-version client regressions passed for fresh capability negotiation,
  deletion-only durable ACKs, retained viewing intents and rejection of implicit
  legacy-read fallback. Orange double checks now require at least one actual read
  confirmation; group labels expose the read count and recovery continues for
  remaining original recipients. Follow-up regressions passed for a single
  confirmed reader with other recipients pending, continued group status recovery
  and legacy automatic-read rejection. The presence cancellation follow-up below
  supersedes the earlier backend cancellation coverage.
  The messaging layout keeps technical IDs out of ordinary message headers and
  uses bubble visibility, rather than header visibility, for the viewing dwell.
  Timestamp formatting changes only presentation, preserving UTC history order.
  Messaging UI regressions passed 12 Python template and 167 JavaScript cases,
  Ruff, client syntax, whitespace and release-note checks. They cover merged SVG
  state transitions, compact staggered checks, matching metadata text sizes, sender
  grouping, short/multiline/long content preservation, final-line status space,
  local timezone/locale formatting, viewport placement, mobile keyboard access,
  backdrop/Escape closure and focus restoration. Bubble layout uses shared theme
  rules and preserves the status control's focus/click behavior and hit area.
  Browser rendering and real assistive-technology behavior remain unverified;
  no browser was used.

- **Client redesign, 2026-10-07:** non-browser regressions cover full participant
  values/copying, duplicate names, local-pin terminology, visible changed-key
  blocks (including during a send), native dialog opening/focus restoration,
  mobile view/draft preservation, viewport resizing, appearance persistence/OS
  changes, unchanged errors, pending retries and reset safeguards. Entry regressions
  cover slow saved identity/history reads without activation-form flashes, fresh
  devices, visible storage errors and all invite statuses/personal/group/password
  guidance. Admin sign-in matches its original template exactly. Integration checks
  cover inline alerts, preserved admin CSRF/fields and bundled fonts under the
  client CSP. The full redesign run passed 199 isolated Python and 105 JavaScript
  cases; subsequent affected checks passed all 94 client JavaScript cases and 26
  Python template/smoke/release cases. The unchanged non-client JavaScript results
  remain valid. Ruff, compilation, script syntax, documentation links and
  whitespace passed. Read-only local HTTP checks confirm the running server
  serves the updated client/invite pages and original admin sign-in without a restart.
  Mobile header controls retain accessible names, decorative icons and 40px touch
  targets; connection/security labels stay visible. Desktop styling is preserved.
  Mobile navigation/scroll regressions model hidden zero-size message panels and
  clamped scroll ranges, selected-chat reloads using encrypted history (including
  offline restoration), latest-message opening/reopening, Back-view restoration,
  foreign/malformed/unavailable navigation state and continued invite activation.
  Polling and viewport changes preserve older reading positions while active.
  Existing error mappings and error strings were compared with the base source
  and are unchanged. Browser rendering, real keyboard/assistive technology
  behavior and WCAG conformance have not been independently verified.
- **Unchanged protocol protections, 2026-10-07:** previously passing dependency,
  compilation and regression checks remain valid. Coverage includes native point
  validation, independent OpenSSL encryption/decryption, non-exportable keys,
  malformed and retired-key rejection, blocked mixed rooms, failed-storage
  admission and import rejection before replace-mode mutation. No browser
  verification was performed; Docker was unavailable for a local container build.
  Current client error regressions verify HTTP explanations,
  private validation-value filtering, unknown exceptions,
  storage/encryption failures and retained refresh proposals after a lost response.
- **Release automation:** version/notes validation, workflow configuration,
  Bash syntax and nine mocked publication scenarios passed locally, including
  outdated commits, existing releases, conflicting tags and command failures.
  Repository permissions and remote publication were not verified in that local
  review.
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
- **Deployment, 2026-10-07:** deployed 0.7.0 from exact commit `9876830` after
  successful CI and automatic promotion. The prior live version was 0.6.0; this
  rollout required no key or room migration. A verified SQLite backup and the
  previous image were retained before activation. Read-only checks confirmed
  container/public HTTPS health, exact image revision, matching client assets
  and fonts, updated invite landing, no-store/CSP/no-referrer headers, and public
  admin/schema blocking for HTTP and WebSocket upgrades. Private admin sign-in
  remained available with its original form. Environment, private Compose
  configuration and persistent mounts were unchanged; database integrity and
  user/chat/admin counts were preserved. Application logging remains disabled,
  and no OOM kill was reported. No production message exchange or browser QA was
  performed. The local development service was not restarted.
- **Limits:** tests use isolated databases and Node adapters for browser storage,
  not a live browser. They do not establish native interoperability, production
  configuration, penetration/load-test results, a full dependency audit or a
  cryptographic proof.

For future updates, replace the current status and latest verification record
when new evidence exists. Keep unresolved boundaries and acceptance criteria;
put release history in the changelog instead of appending development diaries.
