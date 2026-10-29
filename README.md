# Sideword Server

[![Tests](https://github.com/vladleesi/sideword-chat-server/actions/workflows/test.yml/badge.svg)](https://github.com/vladleesi/sideword-chat-server/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Self-hosted FastAPI/SQLite backend for invite-only personal and group chats.
Clients encrypt content; the relay removes ciphertext after durable ACK or expiry.
Includes expiring/password-protected invites, renewable sessions, WebSocket/polling
delivery, confirmations, an admin UI and a browser test client with 1 MiB attachments.

The client is unaudited. Pre-releases require fresh server/browser state and matching
assets; read [security limits](SECURITY.md) and [upgrade steps](docs/UPGRADING.md#upgrade).

## Setup

Clone the repository and copy [.env.example](.env.example) to `.env`. Set
`SIDEWORD_ADMIN_USERNAME`, `SIDEWORD_ADMIN_PASSWORD` and a random secret:

```sh
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

Use that output for `SIDEWORD_SECRET_KEY`; keep `.env` private.

### Run locally

Python 3.12+:

```sh
python -m venv .venv
# Activate .venv, then:
python -m pip install -r requirements.txt
python -m scripts.serve_shared
```

Loopback **8000** includes administration; **8001** blocks admin/schema routes.

### Docker

```sh
docker compose up -d --build
```

Requires `.env` and Compose v2. Loopback **8000** includes administration.
Data uses `sideword-data`; exports use `./exports`. `docker compose down -v` deletes data.

### Open the server

On port 8000: `/client` for chat, `/admin/links` for invites, `/docs` for OpenAPI,
and `/health` for health/version. Create or reset an admin with
`python -m scripts.create_admin --username admin` (Docker: prefix with
`docker compose exec sideword`). Passwords are limited to 72 UTF-8 bytes;
resets revoke admin sessions. Remove bootstrap credentials after account creation.

## Public access

Set `SIDEWORD_PUBLIC_URL=https://chat.example.com` and use an HTTPS/WSS proxy to
restricted port **8001**. Docker's **8000** needs explicit admin/schema blocking.
Follow [deployment controls](docs/UPGRADING.md#public-deployment-controls).

Create invites in the admin UI; share passwords separately. Verify peer fingerprints
through another trusted channel. Clearing browser state can permanently lose keys/history.
See [access and devices](docs/SESSION_LIFECYCLE.md).

## Reference

[API](docs/API.md) · [Protocol](docs/PROTOCOL.md) · [Operations](docs/UPGRADING.md) ·
[Security review](docs/SECURITY_REVIEW.md) · [Contributing](CONTRIBUTING.md) ·
[Changelog](CHANGELOG.md) · [Conduct](CODE_OF_CONDUCT.md) · [License](LICENSE) ·
[Third-party notices](THIRD_PARTY_NOTICES.md)
