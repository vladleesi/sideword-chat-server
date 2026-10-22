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
pending deliveries. The CLI exporter uses the same current configuration format,
including protected-invite and resume verifiers. Both exports contain secrets and
need private storage.

Browser keys/history are separate and origin-bound. Server backups cannot recover
them. Clearing site data or Reset device can permanently lose them without freeing
participant slots; new invites may be needed.

## Upgrade

Each pre-release requires a fresh database and browser device state; data and
clients from a different pre-release are unsupported. Preserve backups and saved
device data separately before starting fresh. Clearing browser storage permanently
loses access to its message keys and history.

1. Read the target [changelog](../CHANGELOG.md), stop all writers and take a backup.
2. Choose a new database path or Docker volume. Keep the existing database and
   browser data separately; do not erase them as part of setup.
3. Install matching backend code, dependencies and browser assets using the
   [setup instructions](../README.md#setup). Review `.env.example` without replacing
   existing secrets. Follow the [asset cache controls](#public-asset-transfers)
   when deploying updated templates and files.
4. Start the new installation with fresh browser device state and create new
   invites. Check `/health` for the expected version and verify private routes
   are blocked publicly.
5. Keep the backup until verification is complete.

Startup creates the current schema and rejects incompatible existing schemas
without rewriting them. Use a new database path or volume instead of deleting a
database in place. SQLite 3.35+ is required for atomic delivery acknowledgement.
Backend release, HTTP API and encryption envelope versions are independent.
GitHub release publication does not deploy the backend or Pages; see
[release automation](../CONTRIBUTING.md#pull-requests-and-releases).

Queued ciphertext, retry/session records and persistent user/invite metadata have
separate lifecycles; see [retention boundaries](SECURITY_REVIEW.md#delivery-local-storage-and-retention).

## Restore safely

Restore only with the matching pre-release code and client assets. A backup
restores authentication and policy state, potentially reviving
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

Use the target pre-release's matching server code, client assets and backup.
Later data and protections may be lost; cross-release conversion is unsupported.
Test the rollback offline and apply the restore credential-invalidation procedure
before reopening access. Current-code restore tests do not verify historical
releases or operator backups.

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

The bundled client gives requests, including response bodies, a
15-second deadline; an interrupted response may already have been processed by
the server. Retrying the persisted encrypted outbox preserves its message ID and
recipient ciphertext. Separate-origin identities/history stay separate; see
[device lifecycle](SESSION_LIFECYCLE.md#persistence-devices-and-reconnects).

### Public asset transfers

Bundled `/static/` files use gzip when supported and
`Cache-Control: public, max-age=3600, must-revalidate` for successful and
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

Optional participant presence is in-memory and requires one shared server process;
disable `SIDEWORD_PRESENCE_ENABLED` on every independent worker/replica to show
unknown rather than incomplete online counts. Preserve Uvicorn's transport
ping/pong interval and timeout for transport cleanup. See the
[presence contract](API.md#participant-presence) for client heartbeat requirements.
