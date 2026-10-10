# Backups, upgrades and recovery

Use this guide for operator procedures; see [README.md](../README.md) for initial
setup, [CHANGELOG.md](../CHANGELOG.md) for releases, and
[SECURITY_REVIEW.md](SECURITY_REVIEW.md) for security boundaries and outstanding work.

## Backups

Stop all backend writers, then copy the SQLite database and adjacent `-wal` and
`-shm` files before restarting. Defaults are `data/sideword.sqlite3` locally and
`/data/sideword.sqlite3` in Docker's `sideword-data` volume. Preserve configuration
and the signing secret securely with the backup. Encrypt backups with
operator-controlled keys, restrict access, and set separate backup/export retention.
SQL deletion and message TTL do not erase old backups, WAL or free pages.

Configuration transfer is not a full backup. **Admin > Export** at
`/admin/export-ui` includes users, chats, participants, invites, password verifiers
and admission retry metadata. It omits queued messages/receipts, admin accounts and
the send/session ledger. Full replacement removes existing configuration and
pending deliveries. The CLI exporter also omits protected-invite and resume
metadata; use the admin export for configuration transfer.

Browser keys/history are separate and origin-bound. Server backups cannot recover
them. Clearing site data or Reset device can permanently lose them without freeing
participant slots; new invites may be needed.

## Upgrade

GitHub tags and releases are created automatically after CI checks and promotion
of the exact `develop` commit to `main`. Release notes come from the matching
changelog section; see [CONTRIBUTING.md](../CONTRIBUTING.md) for preparation and
retry requirements. Publication does not update running servers or GitHub Pages.

1. Read the target changelog and compatibility notes below.
2. Stop all writers and take the database/configuration backup.
3. Update the server and matching browser assets. Review `.env.example` for changed
   settings without replacing existing secrets. Change every template reference's
   asset query revision when its file changes; purge affected CDN URLs on rollback
   or when reusing an existing revision.
4. In the Python virtual environment, install changed requirements with
   `python -m pip install -r requirements.txt`, then run
   `python -m scripts.serve_shared`. For Docker, use `docker compose up -d --build`.
5. Check `/health` on the local admin listener for the expected version and verify
   private routes are blocked publicly. Reload browser clients.
6. Keep the backup until verification is complete.

Deploy the backend, dependencies, protocol script, client script and template
together. Backend 0.6.0 intentionally replaces the test encryption protocol;
reload clients and create fresh rooms/invites. The new client uses a separate
P-256 browser database and never reads or erases the prior device database.

Startup applies additive SQLite migrations. Upgrade every writer together;
mixing old and new server code against one database is unsupported.
SQLite 3.35+ is required for atomic read deletion/receipt creation.

## Compatibility notes

| Upgrade | Required action / boundary |
| --- | --- |
| Conversation closure (0.10.0) | Startup adds nullable `chats.closed_at`; existing conversations remain open, including those with revoked invites. Deploy all backend writers and matching client assets together, then reload clients. No keys, sessions or history are replaced. Admin > Chats can close/reopen a conversation independently of its invites. Closure preserves membership and queued data but normal TTL retention continues; reopening can deliver retained queues. Export/import includes closure state. Older servers and clients do not implement this control; do not mix workers or roll back to code that ignores closed state while access is open. |
| Browser key pinning | Deploy `client-protocol.js`, `client.js` and `client.html` together. Existing keys/history remain usable; additive `peer:` records hold pins. Verify peers out of band. Do not clear history to dismiss a changed-key warning. |
| Exact acknowledgements | Upgrade the server before clients. Startup backfills random delivery IDs, preserving ciphertext/timestamps. The bundled client requires exact endpoints and retains failed acknowledgements instead of falling back to legacy deletion. |
| Registered sessions (0.3.0) | Startup adds retry/session/rotation/login-limit tables. Admins using old stateless tokens must log in again; cookie-based forms/APIs need CSRF tokens. Browser session/outbox coordination requires Web Locks and fails closed without them. |
| Admin/client race fixes (0.3.1–0.3.2) | No schema, key or client migration. Valid sessions remain usable. New session lifetime is capped by invite expiry; rejected session issuance may follow an already committed admission, so preserve resume credentials. |
| Invite navigation (0.3.3) | Reload the client. A different invite shows its join form and pauses that tab's background activity; activation selects its room. Same invite or `/client` resumes the saved room. Older identities show the form once for an explicit URL. Keys/history/outbox remain scoped to their participant identity for later reconnects. |
| Activation hardening (0.4.0) | Deploy the backend and bundled client together; update custom clients to send the invite token in the JSON body of `POST /api/v1/links/activate`. The old path-based route is removed and returns 404, with no fallback or redirect. Existing identities, memberships, resume credentials and sessions remain usable; no schema or key migration. Activation requires HTTPS or local loopback even with the global TLS check disabled. Supplied invalid bearer headers return 401; remove the rejected header explicitly to retry with saved resume credentials. The bundled client preserves device/admission state for retry. Suppress/redact invite landing URLs and request bodies at every logging layer. |
| P-256 test protocol (0.6.0, breaking) | Upgrade every backend worker and client together, including the new `cryptography` dependency. Retire old rooms/invites and issue new ones; clients start fresh P-256 identities and verify fingerprints again. `/api/v1` and envelope `v: 1` stay, with a new algorithm/context and validated 65-byte public keys. Old clients cannot authenticate or renew; old rooms cannot admit P-256 members; old exports containing retired keys cannot be imported. There is no ciphertext/key conversion or old-format receive support. The prior browser database and server records are not automatically deleted. Key persistence is checked before admission, and unreadable records in the new store are preserved. |
| Message confirmations (0.9.0) | Startup adds `read_receipts.message_delivery_id` and `send_records.receipt_state_json` without replacing existing rows. Deploy backend and bundled client together; reload clients for the new opt-in ACK/viewing fields and status API. Exact deletion-only acknowledgements remain available; ambiguous deletion routes are removed in 0.11.0. A new client talking to an older backend uses deletion-only exact ACKs and disables unsupported delivery/viewing requests until a fresh `/me` advertises support; missing confirmations stay unconfirmed. Restart old backend processes to enable the full feature, even when static client files already changed. No key or session migration. Original recipient sets survive roster changes; existing auto-read history becomes hidden delivery evidence, not proof of viewing. The ledger now retains per-recipient confirmation metadata until the later of original retry expiry and message TTL; retry expiry itself is unchanged. Backups retain this metadata and require the same private retention controls. |

Backend release, HTTP API and encryption envelope versions are independent.
The 0.6.0 replacement changes the test ciphertext format; it does not introduce
a ratchet or recipient forward secrecy. It does not recover prior unreadable keys.
Unused invites with no room/members can still create a fresh P-256 room. Invitations
for rooms with old participants must be replaced, even when admission capacity remains.
Do not restore an old full database to resume old clients under 0.6.0. Keep backups
for archival/explicit rollback, and test any rollback offline before reopening access.

## Retire legacy clients

Backend 0.11.0 removes legacy JWT issuance/acceptance, `POST /api/v1/sessions`
migration, and the ambiguous `/ack` and `/chats/{chat_id}/read` routes. There is no
compatibility switch. Database rows, encryption keys, and delivery IDs are retained.

1. Before upgrading, update supported clients to persist a session credential on
   activation, use renewable sessions, and send exact ACK/read references. The
   bundled client already does this. Verify refresh retries, offline recovery,
   and concurrent tabs in release QA; third-party client compatibility is not
   established by this repository's tests.
2. Existing registered sessions continue to work. Devices with only a legacy JWT
   must recover through their still-authorized invite and saved resume credential.
   The bundled client pauses a retired login while preserving keys/history.
   Without a valid registered session or saved resume credential, the original
   membership cannot be recovered with a public key alone; create a fresh identity
   through an authorized invite. Keep old device data for local-history access.
3. Remove `SIDEWORD_JWT_TTL_HOURS`, `SIDEWORD_ALLOW_LEGACY_ACK`, and
   `SIDEWORD_LEGACY_TOKEN_DEADLINE` from private runtime configuration; they have
   no effect. Restart/deploy only after reviewing the compatibility impact.
4. Session revocation does not revoke saved invite resume credentials; revoke/delete
   their invite if compromised. Other live sessions retain identity memberships;
   deactivate a compromised user to block identity-wide access. Invite revocation
   is not room termination; see [lifecycle controls](SESSION_LIFECYCLE.md).

Do not shorten the send retry window for existing outboxes without resolving them:
clients retain their original deadlines. Queued data, retry/session records and
persistent user/invite metadata have separate lifecycles; see
[retention boundaries](SECURITY_REVIEW.md#delivery-local-storage-and-retention).

## Restore safely

A backup restores old authentication and policy state, potentially reviving
credentials revoked after the snapshot. Before reopening access:

1. Stop every writer. Restore to the intended database location with access blocked.
2. Rotate `SIDEWORD_SECRET_KEY` and configure the new value on every worker.
3. In one database transaction, delete restored `refresh_uses`, then
   `client_sessions`, then `admin_sessions`.
4. Reapply later invite revocations, conversation closures, user deactivations and admin password resets.
   The snapshot rolled these back too; session cleanup cannot reconstruct them.
5. Restart and verify old admin/client tokens and refresh credentials are rejected.
   Admins log in again; clients recover through still-authorized saved invite
   credentials.

Key rotation alone leaves restored refresh credentials usable. Removing the
registered sessions invalidates their access JWTs and refresh credentials; rotating
the signing key also retires the restored signing secret. Invite resume credentials
and restored passwords remain valid unless separately changed.

Full backups preserve delivery IDs and existing send evidence, but may contain
already-acknowledged deliveries. Clients must deduplicate local history before
acknowledging restored rows. Evidence of sends after the snapshot is lost; do not
blindly replay later outboxes. Exercise this procedure with an isolated copy of
your actual backup. Automated restore tests do not validate your storage, secret
distribution, proxy or historical server/client versions.

## Rollback

Use matching server/client code and a compatible pre-upgrade backup; later data
and protections may be lost. Apply the restore credential-invalidation procedure
before reopening. Do not downgrade only the server beneath clients requiring
exact acknowledgements. Older browser code loses peer-pin enforcement and can
delete retained history on participant changes. Rolling back 0.3.1–0.3.2 fixes
restores the documented authentication races. Historical rollback remains an
operator test requirement, not a guarantee from current-code restore tests.

## Public deployment controls

Require HTTPS/WSS outside loopback. Keep `/admin`, its subpaths, `/docs`, `/redoc`
and `/openapi.json` private for HTTP and WS upgrades. The shared runner exposes
only the restricted listener on port 8001; Docker's port 8000 includes admin routes
and needs proxy restrictions.

For multiple public hostnames, route all API and WebSocket paths to one backend
process and database. The connection registry is process-local; independent
workers/replicas do not broadcast to one another. Preserve TLS, any configured
client-certificate verification, trusted proxy headers and private administration
on every path. Domain aliases do not require shared cookies, relaxed CORS or key
transfer. `SIDEWORD_PUBLIC_URL` controls generated invite addresses only.

Backend 0.11.1 refreshes the bundled client's recovery behavior without changing
the API, encryption format or database schema. Deploy the versioned client asset
with its template and reload clients. Requests, including response bodies, have a
15-second deadline; an interrupted response may already have been processed by
the server. Retrying the persisted encrypted outbox preserves its message ID and
recipient ciphertext. Separate-origin identities/history stay separate; see
[device lifecycle](SESSION_LIFECYCLE.md#persistence-devices-and-reconnects).

### Public asset transfers

Backend 0.9.1 compresses bundled `/static/` files with gzip when supported and
returns `Cache-Control: public, max-age=3600, must-revalidate` for successful and
conditional asset responses. Browsers and CDNs may reuse these public files for
one hour; existing ETag/Last-Modified validators support revalidation. The URLs
are not immutable: unversioned and old query revisions still resolve to the
current files. Deploy templates and files together, update every affected query
revision, and retain query strings in the CDN cache key. Purge affected cached
URLs on rollback or when replacing files under an already-used revision.

Scope CDN cache eligibility to public assets and GET/HEAD requests. A
hostname-wide cache-bypass rule must exclude those requests; for Cloudflare,
the bypass expression can be:

```text
(http.host eq "chat.example.com") and not
(starts_with(http.request.uri.path, "/static/") and http.request.method in {"GET" "HEAD"})
```

Respect origin cache headers; never force caching for `/client`, `/l/`, API/admin
responses, redirects or errors. Those responses remain `no-store`. Only bundled
public files receive application gzip compression; do not extend it to responses
containing credentials or user content.

Successful static responses send `X-Accel-Buffering: yes` so Nginx can buffer
asset downloads even if the general proxy disables buffering. Keep this header
enabled for public files; no WebSocket or private-response buffering policy is
changed. Nginx needs no reload for this response-header override unless its
configuration explicitly ignores `X-Accel-Buffering`. See the
[Nginx buffering reference](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_buffering)
and [Cloudflare cache rules](https://developers.cloudflare.com/cache/how-to/cache-rules/).

After deployment, verify gzip content matches the release, a repeated asset
request reports a CDN cache hit, and client/invite/API responses remain uncached.
Compare first-byte and complete-download times; local tests do not establish
production network performance.

### Proxy trust and resource limits

Trust only actual proxy IPs: `SIDEWORD_TRUSTED_PROXY_IPS` for the shared runner,
`FORWARDED_ALLOW_IPS` for standalone Uvicorn/Docker. Never use `*`. Container peers
may differ from localhost. Configure HSTS at the TLS terminator after validation.

Suppress/redact invite paths, query strings, request bodies, authorization,
cookies and WS authentication at every proxy. Uvicorn requires both
`--no-access-log` and `--log-level warning` because WS URLs use its error logger
at INFO; shared runner/Docker already set these. Existing logs are not erased.

HTTP/WS rate, connection and frame limits are per process. Database quotas and
login limits are shared. Add gateway/global limits, header/idle timeouts, and host
CPU/memory/disk limits. Defaults are in [the configuration example](../.env.example)
and [API capacity rules](API.md#send-retries-and-capacity).

Version 0.8.0 adds optional in-memory participant presence without a database or
encryption migration. Accurate presence requires one shared server process;
disable `SIDEWORD_PRESENCE_ENABLED` on every independent worker/replica to show
unknown rather than incomplete online counts. Preserve Uvicorn's transport
ping/pong interval and timeout for legacy socket cleanup. See the
[presence contract](API.md#participant-presence) for client heartbeat requirements.
