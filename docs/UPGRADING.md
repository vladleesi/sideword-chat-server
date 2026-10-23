# Backups, upgrades and recovery

[Setup](../README.md#setup) · [Releases](../CHANGELOG.md) · [Security review](SECURITY_REVIEW.md)

## Backups

Stop all writers, then copy SQLite and adjacent `-wal`/`-shm` files. Defaults:
`data/sideword.sqlite3` locally; `/data/sideword.sqlite3` in Docker's `sideword-data`
volume. Preserve signing/configuration secrets, encrypt backups and set retention.
SQL deletion does not erase backups, WAL or free pages.

Admin/CLI exports are unencrypted configuration, not full backups. They include
users, chats, memberships, invites, password/resume verifiers and admission retry metadata;
they omit pending messages/receipts, admins and send/session ledgers. Replacing
configuration removes current configuration and pending deliveries. Protect exports
as secrets.

Browser keys/history are origin-bound and absent from server backups. Clearing
browser state can permanently lose access without freeing participant slots.

## Upgrade

Each pre-release requires fresh server/browser state and matching code/assets.
Data conversion is unsupported; preserve old state separately.

1. Read the changelog, stop writers and back up.
2. Select a fresh database path or Docker volume; retain old server/browser data.
3. Install matching code/dependencies/assets using [setup](../README.md#setup).
   Review `.env.example` without replacing secrets; apply [cache controls](#public-asset-transfers).
4. Start fresh, create invites and verify `/health` and public admin blocking.
5. Keep backups until verification finishes.

SQLite 3.35+ is required. Startup creates the schema and rejects incompatible
databases without rewriting them. Release publication does not deploy the backend
or Pages; see [release automation](../CONTRIBUTING.md#pull-requests-and-releases).

Manual deployment checks schema/health configuration before stopping the backend.
Use a fresh database filename inside the existing persistent mount for a new
pre-release. Inherit the image's Python health probe; remove `curl` overrides and
never disable health checks. Failed activation stops the candidate and retains
backups/rollback images. Fix configuration before retrying the manual workflow.

## Restore safely

Use matching pre-release code/assets. Restoring a snapshot can revive revoked
credentials and undo later policy changes:

1. Stop writers; restore with external access blocked.
2. Rotate `SIDEWORD_SECRET_KEY` on every worker.
3. In one transaction delete `refresh_uses`, then `client_sessions`, then `admin_sessions`.
4. Reapply later invite revocations, closures, user deactivations and password resets.
5. Restart and verify old tokens/refresh credentials fail. Admins log in again;
   clients recover only through still-authorized invite credentials.

Key rotation/session removal does not invalidate invite resume credentials or
passwords; revoke/change those separately. Snapshots cannot reconstruct later changes.

Full backups retain delivery IDs and ACK/send evidence. Deduplicate restored
messages before ACK; retry newer outboxes carefully because later evidence is
missing. Test an isolated copy of the operator's backup before reopening access.

## Rollback

Restore the target release's matching code/assets/backup. Newer data/protections
may be lost. Test offline and apply [credential invalidation](#restore-safely).

## Public deployment controls

- Require HTTPS/WSS outside loopback. Block `/admin` and descendants, `/docs`,
  `/redoc`, `/openapi.json` for HTTP and WS. Proxy restricted **8001**, or explicitly
  block those routes on Docker's admin-inclusive **8000**.
- Route aliases' API/WS traffic to one process/database. Independent workers do
  not share connections. Aliases need no shared cookies, relaxed CORS or key transfer;
  `SIDEWORD_PUBLIC_URL` only chooses generated invite addresses.
- Trust actual proxy IPs only: `SIDEWORD_TRUSTED_PROXY_IPS` for the shared runner,
  `FORWARDED_ALLOW_IPS` for Uvicorn/Docker; never `*`. Preserve configured client
  certificate checks and enable HSTS after TLS validation.
- Redact invite paths/queries, bodies, Authorization, cookies and WS auth at every
  logging layer. Use `--no-access-log --log-level warning`; WS URLs otherwise reach INFO logs.
- Add gateway limits, header/idle timeouts and CPU/memory/disk limits. HTTP/WS
  limits are per process; DB quotas/login limits are shared. See [.env.example](../.env.example)
  and [API limits](API.md#send-retries-and-capacity).
- Disable `SIDEWORD_PRESENCE_ENABLED` across independent workers. Preserve transport
  ping/pong cleanup; see [presence](API.md#participant-presence).

Interrupted requests may have committed. Retry persisted ciphertext/message IDs
instead of re-encrypting; see [device/reconnect behavior](SESSION_LIFECYCLE.md#persistence-devices-and-reconnects).

### Public asset transfers

Cache only public `/static/` GET/HEAD requests. Preserve gzip, ETag/Last-Modified,
query strings in CDN keys, and `Cache-Control: public, max-age=3600, must-revalidate`.
Old revision URLs serve current files: deploy templates/assets together, advance
query revisions and purge caches when rolling back or reusing a revision.

Example Cloudflare bypass rule:

```text
(http.host eq "chat.example.com") and not
(starts_with(http.request.uri.path, "/static/") and http.request.method in {"GET" "HEAD"})
```

Keep clients, invites, API/admin, redirects and errors `no-store`; never compress
private responses. Allow static `X-Accel-Buffering: yes` without changing private/WS
buffering. See [Nginx](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_buffering)
and [Cloudflare](https://developers.cloudflare.com/cache/how-to/cache-rules/).
Verify asset delivery, CDN hits and private-response headers after deployment;
measure network performance externally.
