# Backups, upgrades and recovery

Initial setup: [README](../README.md). Releases: [CHANGELOG](../CHANGELOG.md).
Security boundaries: [SECURITY_REVIEW](SECURITY_REVIEW.md).

## Backups

Stop all writers, then copy SQLite and adjacent `-wal`/`-shm` files. Defaults:
`data/sideword.sqlite3` locally; `/data/sideword.sqlite3` in Docker's `sideword-data`
volume. Securely preserve configuration/signing secrets, encrypt backups and set
backup/export retention. SQL deletion/TTL does not erase backups, WAL or free pages.

**Admin → Export** (`/admin/export-ui`) and the CLI export configuration, not a
full backup. They include users, chats, members, invites, password/resume verifiers
and admission retry metadata; they omit pending messages/receipts, admins and
send/session ledgers. Full replacement removes existing configuration and pending
deliveries. Treat exports as secrets.

Browser keys/history are separate and origin-bound. A server backup cannot restore
them. Clearing/resetting browser state can permanently lose access without freeing
participant slots.

## Upgrade

Each pre-release requires fresh server/browser state and matching code/assets;
cross-release data conversion is unsupported. Preserve old state separately.

1. Read the target changelog, stop writers and back up.
2. Select a new database path or Docker volume; retain the old database/browser data.
3. Install matching dependencies/backend/assets using [setup](../README.md#setup).
   Review `.env.example` without replacing secrets; apply [cache controls](#public-asset-transfers).
4. Start fresh, create invites, verify `/health` and public admin blocking.
5. Retain the backup until verification finishes.

Startup creates the current schema and rejects incompatible databases without
rewriting them. SQLite 3.35+ is required for atomic ACKs. Release publication
deploys neither backend nor Pages; [release automation](../CONTRIBUTING.md#pull-requests-and-releases)
is separate. Backend/API/encryption versions are independent.

## Restore safely

Use matching pre-release code/assets. Restoring a snapshot can revive revoked
credentials and undo later policy changes. Before reopening access:

1. Stop every writer; restore with external access blocked.
2. Rotate `SIDEWORD_SECRET_KEY` on every worker.
3. In one transaction delete restored `refresh_uses`, then `client_sessions`, then `admin_sessions`.
4. Reapply later invite revocations, closures, user deactivations and admin password resets.
5. Restart; verify old tokens/refresh credentials fail. Admins log in again;
   clients recover through still-authorized saved invite credentials.

Key rotation alone does not invalidate refresh credentials. Session removal does
not invalidate invite resume credentials or passwords; change those separately
when required. Snapshots cannot reconstruct changes made after capture.

Full backups retain delivery IDs and send evidence, including already-ACKed rows.
Deduplicate restored deliveries locally before ACK. Later send evidence is lost;
do not blindly replay newer outboxes. Test an isolated copy of the actual backup;
fixture tests do not validate operator storage, proxy/secrets or historical versions.

## Rollback

Use the target release's matching code/assets/backup; newer data/protections may
be lost. Test offline and apply [restore invalidation](#restore-safely) before reopening.

## Public deployment controls

- Require HTTPS/WSS outside loopback. Block `/admin` and descendants, `/docs`,
  `/redoc`, `/openapi.json` on HTTP and WS upgrades. Proxy the shared runner's
  restricted **8001**, or restrict Docker's admin-inclusive **8000** explicitly.
- Route aliases' API/WS traffic to one process/database. Independent workers do
  not share live connections; aliases need no shared cookies, relaxed CORS or key
  transfer. `SIDEWORD_PUBLIC_URL` only chooses generated invite addresses.
- Trust actual proxy IPs only: `SIDEWORD_TRUSTED_PROXY_IPS` for the shared runner,
  `FORWARDED_ALLOW_IPS` for Uvicorn/Docker; never `*`. Preserve client-certificate
  checks where configured. Enable HSTS at the TLS terminator after validation.
- Redact invite paths/query strings, bodies, Authorization, cookies and WS auth
  at every logging layer. Uvicorn needs `--no-access-log --log-level warning`
  because WS URLs otherwise enter INFO error logs; bundled runners set both.
- HTTP/WS rate/frame/connection limits are per process; DB quotas/login limits
  are shared. Add gateway limits, header/idle timeouts and host CPU/memory/disk
  limits. Defaults: [.env.example](../.env.example), [API limits](API.md#send-retries-and-capacity).
- Presence requires one shared process. Disable `SIDEWORD_PRESENCE_ENABLED` on
  independent workers/replicas; retain Uvicorn transport ping/pong cleanup.
  See [presence](API.md#participant-presence).

Client HTTP/response deadlines are 15 seconds; interrupted requests may have
committed. Retry persisted ciphertext/message IDs rather than re-encrypting.
Device/origin behavior: [lifecycle](SESSION_LIFECYCLE.md#persistence-devices-and-reconnects).

### Public asset transfers

`/static/` supports gzip and `Cache-Control: public, max-age=3600, must-revalidate`,
including conditional responses. Keep ETag/Last-Modified validation and query
strings in CDN cache keys. URLs are not immutable: old revisions serve current
files. Deploy templates/assets together, advance affected query revisions and
purge cached URLs on rollback or reuse of a revision.

Cache only public asset GET/HEAD requests. For example, a Cloudflare bypass rule:

```text
(http.host eq "chat.example.com") and not
(starts_with(http.request.uri.path, "/static/") and http.request.method in {"GET" "HEAD"})
```

Respect origin headers. `/client`, invites, API/admin, redirects and errors remain
`no-store`; do not extend asset compression to credentials/user content.
Successful static responses set `X-Accel-Buffering: yes`; allow this Nginx override
without changing private/WS buffering. See [Nginx](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_buffering)
and [Cloudflare](https://developers.cloudflare.com/cache/how-to/cache-rules/).

After deployment verify asset content/gzip/CDN hits and private-response no-store.
Measure first-byte/download time externally; local tests do not establish network performance.
