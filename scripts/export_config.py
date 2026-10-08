"""CLI: dump server configuration to JSON (no message history)."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db import init_db, session_scope
from app.models import Chat, ChatMember, Link, User


def _dt_iso(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


async def _run(output: Path) -> int:
    await init_db()
    async with session_scope() as session:
        users = list((await session.execute(select(User).order_by(User.id.asc()))).scalars())
        chats = list(
            (
                await session.execute(
                    select(Chat)
                    .options(selectinload(Chat.members).selectinload(ChatMember.user))
                    .order_by(Chat.id.asc())
                )
            ).scalars()
        )
        links = list((await session.execute(select(Link).order_by(Link.id.asc()))).scalars())

    chat_index_by_id = {c.id: i for i, c in enumerate(chats)}
    bundle = {
        "version": 1,
        "generated_at": _dt_iso(datetime.now(timezone.utc)),
        "users": [
            {
                "public_id": u.public_id,
                "display_name": u.display_name,
                "public_key": base64.b64encode(u.public_key).decode("ascii"),
                "created_at": _dt_iso(u.created_at),
                "is_active": u.is_active,
            }
            for u in users
        ],
        "chats": [
            {
                "chat_type": c.chat_type.value,
                "title": c.title,
                "created_at": _dt_iso(c.created_at),
                "closed_at": _dt_iso(c.closed_at),
                "members": [
                    {
                        "public_id": m.user.public_id,
                        "joined_at": _dt_iso(m.joined_at),
                    }
                    for m in c.members
                    if m.user is not None
                ],
            }
            for c in chats
        ],
        "links": [
            {
                "token": link.token,
                "link_type": link.link_type.value,
                "chat_index": chat_index_by_id.get(link.chat_id)
                if link.chat_id is not None
                else None,
                "max_uses": link.max_uses,
                "uses_count": link.uses_count,
                "is_active": link.is_active,
                "note": link.note,
                "created_at": _dt_iso(link.created_at),
                "expires_at": _dt_iso(link.expires_at),
            }
            for link in links
        ],
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {output}")
    return 0


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Export Sideword configuration as JSON.")
    default_path = settings.exports_path / (
        f"sideword-config-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    )
    parser.add_argument("--output", default=str(default_path), help="output file path")
    args = parser.parse_args()
    return asyncio.run(_run(Path(args.output)))


if __name__ == "__main__":
    raise SystemExit(main())
