"""Standard HTTPS password authentication for invite admission (not a PAKE).

The server/TLS terminator is trusted with the submitted password. Only a salted
scrypt verifier is persisted. Passwords are never used as message encryption keys.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
from datetime import datetime, timezone

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from .models import Link

FAILURE_LIMIT = 5
FAILURE_WINDOW_SECONDS = 300


def require_secure_transport(
    request: Request, detail: str = "Room passwords require HTTPS (or local loopback).",
) -> None:
    # Proxy scheme is set only by the server's configured trusted proxy support.
    # Do not read arbitrary X-Forwarded-* headers here.
    local = (
        request.url.hostname in {"localhost", "127.0.0.1", "::1"}
        and request.client is not None
        and request.client.host in {"127.0.0.1", "::1"}
    )
    if request.url.scheme != "https" and not local:
        raise HTTPException(400, detail)


def validate_password(password: str) -> None:
    if not 8 <= len(password) <= 32:
        raise ValueError("Use a phrase or password of 8 to 32 characters.")
    if password.isspace():
        raise ValueError("The room password cannot contain only spaces.")


def _derive(password: str, salt: bytes) -> bytes:
    # OWASP scrypt profile: N=2^17, r=8, p=1 (128 MiB).
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=2**17, r=8, p=1,
        dklen=32, maxmem=256 * 1024 * 1024,
    )


def hash_room_password(password: str) -> str:
    validate_password(password)
    salt = secrets.token_bytes(16)
    return f"scrypt-v1${salt.hex()}${_derive(password, salt).hex()}"


def valid_verifier(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.split("$")
    try:
        return (
            len(parts) == 3 and parts[0] == "scrypt-v1"
            and len(parts[1]) == 32 and len(bytes.fromhex(parts[1])) == 16
            and len(parts[2]) == 64 and len(bytes.fromhex(parts[2])) == 32
        )
    except ValueError:
        return False


def verify_room_password(password: str, verifier: str) -> bool:
    if not valid_verifier(verifier) or len(password) > 128:
        return False
    _, salt, expected = verifier.split("$")
    return hmac.compare_digest(_derive(password, bytes.fromhex(salt)), bytes.fromhex(expected))


def resume_digest(credential: str | None) -> str | None:
    if credential is None:
        return None
    # 32 random bytes, base64url without padding. A high-entropy bearer credential,
    # not a password or a public-key-based claim of identity.
    if len(credential) != 43 or any(
        c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for c in credential
    ):
        raise HTTPException(422, "Invalid resume credential.")
    return hashlib.sha256(credential.encode("ascii")).hexdigest()


async def authenticate_password(
    session: AsyncSession, link: Link, password: str | None,
) -> None:
    """Called under the admission write transaction; failures survive restarts.

    Per-invite throttling cannot be bypassed by rotating source addresses. A fixed
    window bounds lockout; existing authenticated members bypass this gate.
    """
    if link.password_hash is None:
        return
    now = datetime.now(timezone.utc)
    start = link.failed_window_started_at
    if start is not None and start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    age = (now - start).total_seconds() if start else FAILURE_WINDOW_SECONDS
    if age >= FAILURE_WINDOW_SECONDS:
        link.failed_attempts = 0
        link.failed_window_started_at = now
    elif link.failed_attempts >= FAILURE_LIMIT:
        raise HTTPException(
            429, "Too many failed attempts. Try again later.",
            headers={"Retry-After": str(max(1, math.ceil(FAILURE_WINDOW_SECONDS - age)))},
        )
    valid = password is not None and await run_in_threadpool(
        verify_room_password, password, link.password_hash,
    )
    if not valid:
        link.failed_attempts += 1
        await session.commit()
        raise HTTPException(403, "The room phrase or password is incorrect.")
    # A successful join does not erase other failed guesses in this window.
