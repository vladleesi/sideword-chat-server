"""Isolated environment defaults established before test-module imports."""

import os
import secrets
import tempfile
from pathlib import Path

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="sideword-pytest-"))

os.environ.setdefault("SIDEWORD_SECRET_KEY", secrets.token_urlsafe(32))
os.environ.setdefault("SIDEWORD_ADMIN_USERNAME", "admin")
os.environ.setdefault("SIDEWORD_ADMIN_PASSWORD", "adminpass")
os.environ["SIDEWORD_DB_PATH"] = str(_TEST_ROOT / "sideword.sqlite3")
os.environ["SIDEWORD_EXPORTS_DIR"] = str(_TEST_ROOT / "exports")

os.environ["SIDEWORD_REQUIRE_HTTPS"] = "false"
os.environ["SIDEWORD_LOGIN_ATTEMPTS_PER_MINUTE"] = "10000"
os.environ["SIDEWORD_PRIVATE_ADMIN"] = "false"

os.environ["SIDEWORD_MESSAGE_TTL_DAYS"] = "7"
