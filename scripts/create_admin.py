"""CLI: create or rotate an administrator password.

Usage::

    python -m scripts.create_admin --username admin --password ...

Interactive password prompt when ``--password`` is omitted.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import delete, select

from app.db import init_db, session_scope
from app.models import Admin, AdminSession
from app.security import hash_password


async def _run(username: str, password: str) -> int:
    password_hash = hash_password(password)
    await init_db()
    async with session_scope() as session:
        result = await session.execute(select(Admin).where(Admin.username == username))
        admin = result.scalar_one_or_none()
        if admin is None:
            admin = Admin(username=username, password_hash=password_hash)
            session.add(admin)
            print(f"created admin '{username}'")
        else:
            admin.password_hash = password_hash
            await session.execute(delete(AdminSession).where(AdminSession.admin_id == admin.id))
            print(f"updated password for admin '{username}'")
        await session.commit()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or update a Sideword admin account.")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", default=None, help="omit to prompt securely")
    args = parser.parse_args()

    password = args.password or getpass.getpass("Password: ")
    if not password:
        print("empty password rejected", file=sys.stderr)
        return 2
    if len(password.encode("utf-8")) > 72:
        print("Admin passwords must be at most 72 UTF-8 bytes.", file=sys.stderr)
        return 2
    return asyncio.run(_run(args.username, password))


if __name__ == "__main__":
    raise SystemExit(main())
