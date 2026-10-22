"""Configuration safety checks."""

import pytest
from pydantic import ValidationError

from app.config import Settings


@pytest.mark.parametrize(
    "secret",
    [
        "dev-insecure-secret-change-me",
        "change-me-please-use-long-random-string",
        "replace-this-with-output-from-the-command-above",
    ],
)
def test_placeholder_secrets_are_rejected(secret: str) -> None:
    with pytest.raises(ValidationError, match="secret_key"):
        Settings(_env_file=None, secret_key=secret)


def test_example_admin_password_is_rejected() -> None:
    with pytest.raises(ValidationError, match="SIDEWORD_ADMIN_PASSWORD must be replaced"):
        Settings(
            _env_file=None,
            secret_key="a-secure-enough-test-secret-that-is-long",
            admin_username="admin",
            admin_password="change-me-too",
        )


def test_explicit_development_settings_are_accepted() -> None:
    settings = Settings(
        _env_file=None,
        secret_key="a-secure-enough-test-secret-that-is-long",
        admin_username="admin",
        admin_password="a-local-password",
    )

    assert settings.admin_username == "admin"


def test_settings_source_precedence_and_empty_dotenv_value(tmp_path, monkeypatch):
    config = tmp_path / "settings.env"
    config.write_text(
        "SIDEWORD_PUBLIC_URL=https://file.example\n"
        "SIDEWORD_ADMIN_PASSWORD= # intentionally empty\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("SIDEWORD_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("SIDEWORD_PUBLIC_URL", "https://environment.example")
    settings = Settings(_env_file=config)
    assert settings.public_url == "https://environment.example"
    assert settings.admin_password == ""
    settings = Settings(_env_file=config, PuBlIc_Url="https://argument.example")
    assert settings.public_url == "https://argument.example"
