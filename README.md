<p align="center"><img src="docs/mark.svg" alt="Sideword" width="96" height="96"></p>

<h1 align="center">Sideword Chat Server</h1>

<p align="center">
  <a href="https://github.com/vladleesi/sideword-chat-server/releases/latest"><img src="https://img.shields.io/github/v/release/vladleesi/sideword-chat-server?style=flat-square&amp;label=Release" alt="Latest release"></a>
  <a href="https://github.com/vladleesi/sideword-chat-server/actions/workflows/test.yml?query=branch%3Adevelop"><img src="https://img.shields.io/github/actions/workflow/status/vladleesi/sideword-chat-server/test.yml?branch=develop&amp;event=push&amp;label=Tests&amp;style=flat-square" alt="Tests on develop"></a>
  <a href="#run-locally"><img src="https://img.shields.io/badge/Python-3.12%2B-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white" alt="Python 3.12+"></a>
  <a href="https://fastapi.tiangolo.com/"><img src="https://img.shields.io/badge/FastAPI-009688?style=flat-square&amp;logo=fastapi&amp;logoColor=white" alt="FastAPI"></a>
  <a href="https://www.sqlite.org/"><img src="https://img.shields.io/badge/SQLite-003B57?style=flat-square&amp;logo=sqlite&amp;logoColor=white" alt="SQLite"></a>
  <a href="#docker"><img src="https://img.shields.io/badge/Docker-2496ED?style=flat-square&amp;logo=docker&amp;logoColor=white" alt="Docker"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square" alt="License: MIT"></a>
</p>

Self-hosted FastAPI/SQLite backend for invite-only personal and group chats.
Clients encrypt content; the relay removes ciphertext after durable ACK or expiry.
Includes expiring/password-protected invites, renewable sessions, WebSocket/polling
delivery, confirmations, an admin UI and a browser test client with 1 MiB attachments.
Text/cards arrive independently of media transfers; see [media and preview limits](docs/PROTOCOL.md#independent-media-delivery).

The client is unaudited. Pre-1.0 upgrades reset server state; browser keys/history remain.
Use matching assets; see [security limits](SECURITY.md) and [upgrades](docs/UPGRADING.md#upgrade).

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
