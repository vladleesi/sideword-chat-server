"""Security boundaries affected by the ASGI, JWT and bcrypt upgrades."""

import asyncio
import json
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.security import (
    create_admin_token,
    create_client_token,
    decode_admin_token,
    decode_client_token,
    hash_password,
    verify_password,
)
from scripts import create_admin


@pytest.mark.parametrize("password", ["x" * 72, "é" * 36, "a" * 69 + "€"])
def test_new_bcrypt_hashes_preserve_exact_utf8_boundary(password):
    hashed = hash_password(password)
    assert hashed.startswith("$2b$")
    assert hashed != hash_password(password)
    assert verify_password(password, hashed)
    assert not verify_password(password + "x", hashed)
    assert not verify_password("wrong", hashed)


@pytest.mark.parametrize("password", ["x" * 73, "é" * 37, "a" * 71 + "€"])
def test_overlong_password_is_rejected_before_database_access(password, monkeypatch):
    async def forbidden():
        pytest.fail("Invalid credentials must not initialize or change the database")

    monkeypatch.setattr(create_admin, "init_db", forbidden)
    with pytest.raises(ValueError, match="72 UTF-8 bytes"):
        asyncio.run(create_admin._run("test-admin", password))


def test_cli_overlong_password_fails_without_echo_or_database_access(monkeypatch, capsys):
    password = "synthetic-private-" * 6
    monkeypatch.setattr("sys.argv", ["create_admin", "--username", "test-admin"])
    monkeypatch.setattr(create_admin.getpass, "getpass", lambda _: password)
    assert create_admin.main() == 2
    output = capsys.readouterr()
    assert "72 UTF-8 bytes" in output.err
    assert password not in output.err + output.out


@pytest.mark.parametrize("password", ["x" * 73, "a" * 71 + "€"])
def test_truncated_bcrypt_passwords_are_rejected(password):
    legacy = bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt()).decode()
    assert not verify_password(password, legacy)
    assert not verify_password("wrong", legacy)
    replacement = hash_password("new-password")
    assert verify_password("new-password", replacement)
    assert not verify_password(password, replacement)


@pytest.mark.parametrize("hashed", ["invalid", "bcrypt-v1$invalid", "é", "bcrypt-v1$é"])
def test_malformed_password_hash_fails_closed(hashed):
    assert not verify_password("test-password", hashed)


def test_hash_format_wrappers_are_not_supported():
    hashed = hash_password("test-password")
    assert not verify_password("test-password", "bcrypt-v1$" + hashed)


@pytest.mark.parametrize("kind", ["admin", "client"])
def test_jwt_algorithm_expiry_and_required_claims(kind):
    if kind == "admin":
        token = create_admin_token(1, "test-admin", secrets.token_hex(16))
        decode = decode_admin_token
    else:
        token = create_client_token(1, "test-public-id", 1, secrets.token_hex(16))
        decode = decode_client_token
    claims = decode(token)
    assert claims["sub"] == "1"
    assert claims["typ"] == kind
    assert isinstance(claims["iat"], int)
    key = get_settings().secret_key
    for algorithm in ("HS384", "HS512", "none"):
        signing_key = "" if algorithm == "none" else key.ljust(64, "x")
        invalid = jwt.encode(claims, signing_key, algorithm=algorithm)
        with pytest.raises(jwt.InvalidAlgorithmError):
            decode(invalid)
    claims["exp"] = int((datetime.now(timezone.utc) - timedelta(seconds=1)).timestamp())
    with pytest.raises(jwt.ExpiredSignatureError):
        decode(jwt.encode(claims, key, algorithm="HS256"))
    del claims["exp"]
    with pytest.raises(jwt.MissingRequiredClaimError):
        decode(jwt.encode(claims, key, algorithm="HS256"))
    # Non-alphabet junk must not be ignored by the new compact-JWS parser.
    with pytest.raises(jwt.InvalidTokenError):
        decode(token + "!!!!")


@pytest.mark.parametrize("content_type", [None, "text/plain"])
def test_json_activation_requires_content_type_without_echoing_input(content_type):
    body = json.dumps({"token": "synthetic-private-invite"})
    headers = {"Content-Type": content_type} if content_type else {}
    with TestClient(create_app()) as client:
        response = client.post("/api/v1/links/activate", content=body, headers=headers)
    assert response.status_code == 422
    assert "synthetic-private-invite" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_malformed_multipart_login_fails_without_exposing_credentials():
    with TestClient(create_app()) as client:
        client.get("/admin/login")
        response = client.post(
            "/admin/login", content=b"synthetic-private-password",
            headers={
                "Content-Type": "multipart/form-data",
                "X-CSRF-Token": client.cookies.get("sideword_csrf"),
            },
        )
    assert response.status_code == 400
    assert "synthetic-private-password" not in response.text
    assert response.headers["cache-control"] == "no-store"
