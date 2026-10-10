"""Security helpers: password hashing, link tokens, JWT."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from .config import get_settings

_settings = get_settings()

JWT_ALG = "HS256"


def hash_password(password: str) -> str:
    # bcrypt passwords must not exceed 72 bytes.
    pw = password.encode("utf-8")[:72]
    return bcrypt.hashpw(pw, bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        pw = password.encode("utf-8")[:72]
        return bcrypt.checkpw(pw, password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


def generate_link_token() -> str:
    """Cryptographically strong URL-safe invite token."""

    return secrets.token_urlsafe(32)


def generate_public_id() -> str:
    """Human-visible participant identifier."""

    # 12 random bytes → compact hex string.
    return secrets.token_hex(6)


def public_key_fingerprint(public_key: bytes) -> str:
    """SHA-256 hex digest of the public key (admin UI + clients)."""

    return hashlib.sha256(public_key).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_client_token(
    user_id: int, public_id: str, link_id: int, session_id: str,
) -> str:
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "pid": public_id,
        "lid": link_id,
        "sid": session_id,
        "typ": "client",
        "iat": int(_now().timestamp()),
        "exp": int(
            (_now() + timedelta(minutes=_settings.client_access_minutes)).timestamp()
        ),
    }
    return jwt.encode(payload, _settings.secret_key, algorithm=JWT_ALG)


def decode_client_token(token: str) -> dict[str, Any]:
    return jwt.decode(
        token,
        _settings.secret_key,
        algorithms=[JWT_ALG],
        options={"require": ["exp", "sub", "typ", "pid", "lid", "sid"]},
    )


def create_admin_token(admin_id: int, username: str, session_id: str) -> str:
    payload: dict[str, Any] = {
        "sub": str(admin_id),
        "usr": username,
        "sid": session_id,
        "typ": "admin",
        "iat": int(_now().timestamp()),
        "exp": int(
            (_now() + timedelta(hours=_settings.admin_session_ttl_hours)).timestamp()
        ),
    }
    return jwt.encode(payload, _settings.secret_key, algorithm=JWT_ALG)


def decode_admin_token(token: str) -> dict[str, Any]:
    return jwt.decode(
        token,
        _settings.secret_key,
        algorithms=[JWT_ALG],
        options={"require": ["exp", "sub", "typ"]},
    )
