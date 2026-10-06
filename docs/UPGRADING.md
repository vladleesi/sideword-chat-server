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
   settings without replacing existing secrets.
4. In the Python virtual environment, install changed requirements with
   `python -m pip install -r requirements.txt`, then run
   `python -m scripts.serve_shared`. For Docker, use `docker compose up -d --build`.
5. Check `/health` on the local admin listener for the expected version and verify
   private routes are blocked publicly. Reload browser clients.
6. Keep the backup until verification is complete.

The bundled client script URL changes for the sender-label and history-scroll updates so reloads
fetch the new code. Existing incoming group history shows current roster names
when its saved sender identity is available; no new invite or device reset is needed.
Reloading opens restored history at the latest messages.

Startup applies additive SQLite migrations. Upgrade every writer together;
mixing old and new server code against one database is unsupported.
SQLite 3.35+ is required for atomic read deletion/receipt creation.

## Compatibility notes

| Upgrade | Required action / boundary |
| --- | --- |
| Browser key pinning | Deploy `client-protocol.js`, `client.js` and `client.html` together. Existing keys/history remain usable; additive `peer:` records hold pins. Verify peers out of band. Do not clear history to dismiss a changed-key warning. |
| Exact acknowledgements | Upgrade the server before clients. Startup backfills random delivery IDs, preserving ciphertext/timestamps. The bundled client requires exact endpoints and retains failed acknowledgements instead of falling back to legacy deletion. |
| Registered sessions (0.3.0) | Startup adds retry/session/rotation/login-limit tables. Admins using old stateless tokens must log in again; cookie-based forms/APIs need CSRF tokens. Browser session/outbox coordination requires Web Locks and fails closed without them. |
| Admin/client race fixes (0.3.1–0.3.2) | No schema, key or client migration. Valid sessions remain usable. New session lifetime is capped by invite expiry; rejected session issuance may follow an already committed admission, so preserve resume credentials. |
| Invite navigation (0.3.3) | Reload the client. A different invite shows its join form and pauses that tab's background activity; activation selects its room. Same invite or `/client` resumes the saved room. Older identities show the form once for an explicit URL. Keys/history/outbox remain scoped to their participant identity for later reconnects. |
| Activation hardening (0.4.0) | Deploy the backend and bundled client together; update custom clients to send the invite token in the JSON body of `POST /api/v1/links/activate`. The old path-based route is removed and returns 404, with no fallback or redirect. Existing identities, memberships, resume credentials and sessions remain usable; no schema or key migration. Activation requires HTTPS or local loopback even with the global TLS check disabled. Supplied invalid bearer headers return 401; remove the rejected header explicitly to retry with saved resume credentials. The bundled client preserves device/admission state for retry. Suppress/redact invite landing URLs and request bodies at every logging layer. |

Backend release, HTTP API and encryption envelope versions are independent.
No upgrade above introduces a ratchet or changes the v1 ciphertext format.

## Retire legacy clients

After deploying the server protections:

1. Confirm every supported client uses renewable sessions and exact ACK/read.
   Verify persisted refresh proposals, duplicate-send handling, offline recovery
   and concurrent tabs in release QA.
2. Set `SIDEWORD_ALLOW_LEGACY_ACK=false` and a fixed UTC
   `SIDEWORD_LEGACY_TOKEN_DEADLINE`. Until then, legacy tokens bypass per-device
   revocation and legacy deletion endpoints retain ambiguous matching.
3. Keep saved invite resume credentials for authorized recovery. Session revocation
   does not revoke them; revoke/delete the invite if they are compromised.

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
4. Reapply later invite revocations, user deactivations and admin password resets.
   The snapshot rolled these back too; session cleanup cannot reconstruct them.
5. Restart and verify old admin/client tokens and refresh credentials are rejected.
   Admins log in again; clients recover through still-authorized saved invite
   credentials.

Both steps 2 and 3 are necessary: key rotation alone leaves refresh credentials
usable, and session removal alone leaves legacy JWTs usable. Invite resume
credentials and restored passwords remain valid unless separately changed.

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
