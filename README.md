# Sideword Server

[![Tests](https://github.com/vladleesi/sideword-chat-server/actions/workflows/test.yml/badge.svg)](https://github.com/vladleesi/sideword-chat-server/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Sideword is a self-hosted messenger backend built with FastAPI and SQLite. It
supports invite-only personal and group chats, optional room passwords, expiring
invites, and an admin UI. Clients encrypt messages, while the server relays
ciphertext and deletes it after acknowledgement, reading, or expiry.

The bundled browser client helps test deployments and client integrations. The
protocol has not been independently audited; see the [security policy](SECURITY.md).

## Setup

Clone the repository, then choose Python or Docker below. Neither requires a
frontend build.

```sh
git clone https://github.com/vladleesi/sideword-chat-server.git
cd sideword-chat-server
```

Copy `.env.example` to a new file named `.env` using your editor or file manager.
Set `SIDEWORD_ADMIN_USERNAME` and replace `SIDEWORD_ADMIN_PASSWORD` with your
admin credentials. Set `SIDEWORD_SECRET_KEY` to a cryptographically random value
of at least 32 characters. With Python installed, generate one using:

```sh
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

Without a local Python installation, run the same command prefixed with
`docker run --rm python:3.12-slim`. Paste the generated value into `.env` before
starting the server, and keep that file out of version control.

### Run locally

Use Python 3.12 or newer and create a virtual environment:

```sh
python -m venv .venv
```

Activate `.venv` using your shell's virtual-environment activation method, or
select its Python interpreter in your IDE. The following commands assume
`python` uses that environment; if your Python command is named `python3`, use
that name instead.

```sh
python -m pip install -r requirements.txt
python -m scripts.serve_shared
```

The shared runner binds both listeners to loopback: port **8000** includes
administration, while port **8001** serves the public API and browser client
with administration blocked.

### Docker

With Docker and Compose v2 installed and `.env` configured, build and start the
server:

```sh
docker compose up -d --build
```

Compose exposes the server on loopback port **8000**, including administration.
The service is `sideword`, its database persists in the `sideword-data` volume,
and exports are mounted to the local `exports/` directory. Use
`docker compose stop` and `docker compose start` to stop and resume it.
`docker compose down` preserves the database volume; adding `-v` deletes it.

### Open the server

Use these paths on localhost, port **8000**, for either installation method:

| Path | Purpose |
| --- | --- |
| `/admin/links` | Sign in with your configured admin credentials and create invites |
| `/client` | Test the browser client |
| `/docs` | Explore the interactive API reference |
| `/health` | Check server health and the backend version |

## Configuration

Settings can be supplied through environment variables or `.env`. Every setting
below uses the `SIDEWORD_` prefix, for example `SIDEWORD_MESSAGE_TTL_DAYS=30`.

| Setting | Default / purpose |
| --- | --- |
| `SECRET_KEY` | Required random signing secret, at least 32 characters |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | Create an admin on first startup |
| `PUBLIC_URL` | Invite origin; defaults to HTTP on localhost, port 8000 |
| `DB_PATH` | `./data/sideword.sqlite3`; `/data/sideword.sqlite3` in Docker |
| `EXPORTS_DIR` | `./exports`; `/exports` in Docker |
| `JWT_TTL_HOURS` | `720`; legacy client JWT lifetime in hours |
| `ADMIN_SESSION_TTL_HOURS` | `12`; admin session lifetime in hours |
| `MESSAGE_TTL_DAYS` | `30`; pending ciphertext lifetime in days |
| `MAX_CIPHERTEXT_BYTES` | `65536`; maximum ciphertext size per envelope |
| `CLIENT_ACCESS_MINUTES`, `CLIENT_SESSION_DAYS` | `15`, `30`; renewable access and absolute session lifetime |
| `SEND_IDEMPOTENCY_DAYS` | `30`; send retry metadata retention after first acceptance |
| `ALLOW_LEGACY_ACK`, `LEGACY_TOKEN_DEADLINE` | `true`, unset; explicit compatibility sunset controls |
| `REQUIRE_HTTPS`, `PRIVATE_ADMIN` | `true`; TLS outside loopback and local-only administration |
| `MAX_REQUEST_BYTES`, `REQUESTS_PER_MINUTE` | `2097152`, `600`; body and per-IP/process request limits |
| `MAX_PENDING_MESSAGES`, `MAX_PENDING_BYTES` | `10000`, `268435456`; shared queued ciphertext limits |

For other limits and operator procedures, see [.env.example](.env.example) and
the [upgrade guide](docs/UPGRADING.md).
Bootstrap credentials can be removed from `.env` after the admin account exists.
To create an account or reset its password, run the interactive command:

```sh
python -m scripts.create_admin --username admin
```

For Docker, prefix it with `docker compose exec sideword`.
Password resets revoke existing admin sessions and reject in-flight logins that
verified the previous password. Sign in again after a reset.
Follow the [restore procedure](docs/UPGRADING.md#restore-safely) when restoring
backups so old credentials do not regain access.

## Public access

Set `SIDEWORD_PUBLIC_URL=https://chat.example.com` to your public HTTPS origin
before starting the server so generated invitations use the correct address.
Configure an HTTPS reverse proxy or tunnel with WebSocket forwarding, and trust
forwarded scheme headers only from the actual proxy address.

| Installation | Proxy target | Administration |
| --- | --- | --- |
| Shared Python runner | Loopback port 8001 | Blocked on 8001; available locally on 8000 |
| Docker Compose | Loopback port 8000 | Proxy must block `/admin` and its subpaths, `/docs`, `/redoc`, and `/openapi.json` |

Keep administration private. Protected invites require HTTPS except during
direct loopback development. A temporary tunnel can forward to port 8001 of the
shared runner; update `SIDEWORD_PUBLIC_URL` and restart the backend whenever its
public origin changes. A named tunnel with a personal domain provides a stable
address. Keep tunnel addresses and credentials out of version control.

Invite paths and session credentials must not enter logs. Apply the
[public deployment controls](docs/UPGRADING.md#public-deployment-controls) for
proxy trust, log suppression, resource limits and backup protection.

## Invites and chats

In **Admin > Invite links > Create link**, choose a personal room with two
participant slots or a group with an optional limit. Server capacity limits
also apply; see [activation rules](docs/API.md#activate-link).
You can leave the room unprotected, generate a 16-character password, or set a
custom phrase of 8-32 characters. Custom phrases match exactly, including case
and whitespace. Share passwords separately from invite links.

Opening an invitation or a link preview does not claim a slot; participants
must explicitly activate it, including the creator if they want to join.
Opening a different invite in the test client shows its join form and pauses
background chat activity in that tab until activation. Successful activation
selects the invited room. The same invite or `/client` resumes the saved room;
device keys and encrypted history are preserved without using **Reset device**.
Full rooms stop accepting new participants, while authenticated reconnects
reuse existing slots. Revocation, deletion, participant deactivation, and
expiry invalidate access. Losing browser credentials does not free a slot.

To test a chat, open the invite in two separate browser profiles or a normal and
private window, activate both participants, and compare peer key fingerprints
out of band. Device keys and decrypted history stay in the browser; **Reset
device** permanently removes them. The latest protected invite is saved encrypted
in the same tab for up to 24 hours, including across refreshes, but closing the
tab or clearing browser data can lose it.

Peer pinning detects later key changes, not initial substitution or malicious
served code. V1 has no recipient forward secrecy or post-compromise recovery;
see the [security review](docs/SECURITY_REVIEW.md) for current protections and open work.
Room passwords control admission, not encryption; the server/TLS terminator sees
them. Every activation requires HTTPS outside loopback, including unprotected
rooms. Invalid supplied bearer authentication returns 401 without claiming a
participant slot; credential-only recovery must omit that header.
The bundled client sends invite tokens in the JSON body of
`POST /api/v1/links/activate`, the only activation route. Clients using the old
path-based route must migrate; it returns 404. Invite landing URLs and request
bodies still require log suppression or redaction.
See [activation rules](docs/API.md#activate-link) for verification, throttling
and reconnect credentials.

## Further documentation

- [Client API](docs/API.md) explains encryption, delivery, and acknowledgement rules.
- [Browser protocol](docs/PROTOCOL.md) defines v1 interoperability and client responsibilities.
- [Backups and upgrades](docs/UPGRADING.md) covers configuration exports, storage, and migrations.
- [Contributing](CONTRIBUTING.md) covers development setup, checks, and releases.
- [Changelog](CHANGELOG.md), [Security](SECURITY.md), [Code of conduct](CODE_OF_CONDUCT.md), and [MIT license](LICENSE).
