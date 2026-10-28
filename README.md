# Sideword Server

[![Tests](https://github.com/vladleesi/sideword-chat-server/actions/workflows/test.yml/badge.svg)](https://github.com/vladleesi/sideword-chat-server/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Sideword is a self-hosted FastAPI/SQLite backend for invite-only personal and group
chats. Clients encrypt messages; the server relays ciphertext and deletes it after
durable acknowledgement or expiry.

- Expiring invites, optional room passwords, participant limits and per-chat names.
- Renewable sessions, WebSocket delivery, HTTP polling and optional presence.
- Separate delivery/viewing confirmations, retry protection and an admin UI.
- Python or Docker deployment; no frontend build.

The bundled `/client` is an unaudited test client. Each pre-release requires fresh
server/browser state. See the [security limits](SECURITY.md) and
[backup and upgrade procedure](docs/UPGRADING.md#upgrade) before replacing an installation.

## Setup

```sh
git clone https://github.com/vladleesi/sideword-chat-server.git
cd sideword-chat-server
```

Copy [.env.example](.env.example) to `.env`. Set `SIDEWORD_ADMIN_USERNAME`,
`SIDEWORD_ADMIN_PASSWORD` and a random `SIDEWORD_SECRET_KEY` of at least 32 characters:

```sh
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

Without local Python, prefix that command with `docker run --rm python:3.12-slim`.
Keep `.env` private. Other settings and defaults are in `.env.example`.

### Run locally

Use Python 3.12+; create and activate a virtual environment, then run:

```sh
python -m venv .venv
# Activate .venv using your shell, or select its interpreter in your IDE.
python -m pip install -r requirements.txt
python -m scripts.serve_shared
```

Both listeners bind to loopback: **8000** includes administration; **8001** blocks
admin/schema routes and is the public proxy target.

### Docker

With Docker Compose v2 and `.env` configured:

```sh
docker compose up -d --build
```

Service `sideword` binds loopback **8000**, including administration. Its database
uses volume `sideword-data`; exports use `./exports`. `docker compose down`
preserves the database; **adding `-v` deletes it**. Rebuild to change Python
packages: the runtime image omits pip/ensurepip and uses a Python health probe.

### Open the server

| Local path on port 8000 | Purpose |
| --- | --- |
| `/admin/links` | Create and manage invites |
| `/client` | Test the client |
| `/docs` | OpenAPI reference |
| `/health` | Health and backend version |

Create/reset an admin interactively with `python -m scripts.create_admin --username admin`
(prefix with `docker compose exec sideword` for Docker). Passwords must fit within
72 UTF-8 bytes; resets revoke admin sessions. Remove bootstrap credentials from
`.env` after the account exists.

## Public access

Set `SIDEWORD_PUBLIC_URL=https://chat.example.com`. Use an HTTPS/WSS proxy, trust
only its actual IPs and keep administration private. Proxy the shared runner's
8001 listener; Docker's 8000 listener requires explicit admin/schema blocking.
Aliases must route HTTP and WebSocket traffic to one backend process/database.
See [deployment controls](docs/UPGRADING.md#public-deployment-controls) for logging,
proxy trust, caching, resource limits and presence configuration.

## Invites and chats

Create a personal or group invite in the admin UI; share any password separately.
Opening a link claims no slot. Compare peer fingerprints through a trusted channel.
Each browser origin has independent keys/history; clearing site data or resetting
the device can permanently lose both. Invite revocation, session revocation and
conversation closure have different effects; see the [lifecycle reference](docs/SESSION_LIFECYCLE.md).

## Reference

- [API](docs/API.md) · [Encryption protocol](docs/PROTOCOL.md)
- [Backups, upgrades and deployment](docs/UPGRADING.md)
- [Security policy](SECURITY.md) · [Security review](docs/SECURITY_REVIEW.md)
- [Contributing and CI](CONTRIBUTING.md) · [Changelog](CHANGELOG.md)
- [Code of conduct](CODE_OF_CONDUCT.md) · [MIT license](LICENSE) · [Third-party notices](THIRD_PARTY_NOTICES.md)
