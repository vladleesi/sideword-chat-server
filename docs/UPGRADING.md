# Backups, upgrades and recovery

[Setup](../README.md#setup) · [Changelog](../CHANGELOG.md) · [Security](SECURITY_REVIEW.md)

## Backups

```sh
python -m scripts.deploy backup --source data/sideword.sqlite3 --destination backup.sqlite3
```

Creates a new private file with configuration, credentials and retry/ACK metadata,
but no queued content or deleted free pages. Deployment backups use the same command.
Protect/encrypt backups and set retention: they still contain secrets. Configuration
exports are unencrypted and omit both sessions and delivery evidence. Neither includes
browser keys/history. Ordinary restarts retain pending content; backup restores do not.

Exclude live database/journals from volume snapshots and backup agents if content must
not be retained there. Existing copies are not retroactively scrubbed. Application deletion
cannot guarantee physical erasure from SSDs, old backups, snapshots or host memory.

## Upgrade

Each pre-release requires fresh server/browser state and matching code/assets; no
automatic migration. SQLite 3.35+ is required; incompatible schemas and WAL databases
are rejected. Version 0.15.0 adds payload v1 and fixed content deadlines; older raw-text
clients are incompatible. Browser DB `sideword-test-client-p256-payload-v1` leaves old
browser data untouched. Configuration bundles remain v2.

1. Stop writers and back up; preserve old server/browser state separately.
2. Choose a fresh database path/volume and install matching code/assets.
3. Review [.env.example](../.env.example) without replacing secrets. Old 64 KiB envelope/
   2 MiB send overrides reject larger attachments; default content lifetime is seven days.
4. Start fresh, create invites, verify `/health`, public admin blocking and cache headers.

Manual deployment checks schema/health before stopping the old backend. Inherit the
image's Python health probe; do not disable it or replace it with `curl`. Failed activation
retains backups/rollback images. Releases deploy neither backend nor Pages.

Publication accepts Docker config or manifest/index image IDs and binds the immutable
registry reference to the checked CI image. Single-image indexes may include BuildKit
provenance; ambiguous multi-platform indexes are rejected. Host verification uses the
resolved config digest and rechecks the registry binding before activation.

## Restore safely

Snapshots can revive revoked credentials and undo later policy changes:

1. Stop writers; restore matching code/assets/data with external access blocked.
2. Rotate `SIDEWORD_SECRET_KEY` on every worker.
3. In one transaction delete `refresh_uses`, then `client_sessions`, then `admin_sessions`.
4. Reapply invite revocations, closures, deactivations and password resets made since backup.
5. Verify old JWTs/refresh credentials fail and test recovery before reopening access.

Invite resume credentials/passwords survive key rotation; revoke/change them separately.
Backups retain delivery IDs/evidence but omit pending content; affected sends show
unavailable. Older snapshots also lose later retry evidence: verify before resending.

## Rollback

Use the target release's matching code/assets/backup, test offline and follow
[credential invalidation](#restore-safely). Newer data/protections may be lost.

## Public deployment controls

- Require HTTPS/WSS. Block `/admin` and descendants, `/docs`, `/redoc`, `/openapi.json`
  over HTTP **and WS**. Proxy restricted **8001**; Docker's **8000** includes admin.
- Route aliases to one process/database. Independent workers do not share connections;
  disable presence across them. Trust only actual proxy IPs in `SIDEWORD_TRUSTED_PROXY_IPS`
  (shared runner) or `FORWARDED_ALLOW_IPS` (Uvicorn/Docker), never `*`.
- Redact invite URLs, request bodies, Authorization, cookies and WS auth at every layer.
  Use `--no-access-log --log-level warning`; WS URLs otherwise reach INFO logs.
- Set gateway/time/resource limits. Allow send bodies up to 10 MiB, other bodies 2 MiB,
  outbound WS 4 MiB. Group copies multiply resources; four HTTP operations can hold
  40 MiB of bodies before additional JSON/base64/ORM/WS copies. Measure runtime memory
  and set host budgets; wire limits are not RSS guarantees. Lower limits may reject files.
- Preserve transport ping/pong, TLS certificate checks and private no-store protections.
  See [API limits](API.md#send-retries-and-capacity) and [.env.example](../.env.example).

### Public asset transfers

Cache/compress only public `/static/` GET/HEAD responses; retain gzip, ETag/Last-Modified,
query-string cache keys and `public, max-age=3600, must-revalidate`. Deploy templates/assets
together and advance revisions; old revision URLs serve current files. Purge reused revisions
on rollback. Keep client/invite/API/admin/redirect/error responses `no-store`, uncompressed,
and preserve WS/private buffering rules. Verify proxy/CDN behavior after deployment.
