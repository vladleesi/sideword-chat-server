"""End-to-end smoke test for critical server flows.

Emulates clients with random 32-byte keys (same wire shape as X25519 public keys),
exercises invite links, ciphertext relay, read receipts, and config export/import.

How to run:


    python -m pytest tests -x

Or execute this module directly.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import tempfile
from pathlib import Path

os.environ.setdefault("SIDEWORD_SECRET_KEY", secrets.token_urlsafe(32))
os.environ.setdefault("SIDEWORD_ADMIN_USERNAME", "admin")
os.environ.setdefault("SIDEWORD_ADMIN_PASSWORD", "adminpass")

_TMPDIR = tempfile.mkdtemp(prefix="sideword-test-")
os.environ["SIDEWORD_DB_PATH"] = str(Path(_TMPDIR) / "sideword.sqlite3")
os.environ["SIDEWORD_EXPORTS_DIR"] = str(Path(_TMPDIR) / "exports")

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.main import create_app  # noqa: E402


def _random_keypair() -> tuple[bytes, bytes]:
    """Pseudo X25519 public keys — random 32-byte blobs."""

    return secrets.token_bytes(32), secrets.token_bytes(32)


async def _run() -> None:
    app = create_app()

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://test") as c:
            # ---------- Admin login ----------
            await c.get("/admin/login")
            c.headers["X-CSRF-Token"] = c.cookies.get("sideword_csrf")
            r = await c.post(
                "/admin/login",
                data={"username": "admin", "password": "adminpass"},
                follow_redirects=False,
            )
            assert r.status_code == 303
            await c.get("/admin/login")
            c.headers["X-CSRF-Token"] = c.cookies.get("sideword_csrf")
            cookie = r.cookies.get("sideword_admin_session")
            assert cookie, "admin cookie missing"
            c.cookies.set("sideword_admin_session", cookie)

            # ---------- Create personal link ----------
            r = await c.post(
                "/admin/links",
                data={"link_type": "personal", "note": "alice<->bob", "expires_in_hours": ""},
                follow_redirects=False,
            )
            assert r.status_code == 303, r.text

            # ---------- Create group link ----------
            for expiry in ("0", "8761", "1.5", "invalid"):
                invalid = await c.post(
                    "/admin/links",
                    data={"link_type": "group", "expires_in_hours": expiry,
                          "group_title": "Keep my title"},
                )
                assert invalid.status_code == 422
                assert invalid.headers["content-type"].startswith("text/html")
                assert "Enter a whole number from 1 to 8760." in invalid.text
                assert "Keep my title" in invalid.text

            r = await c.post(
                "/admin/links",
                data={"link_type": "group", "group_title": "Ops"},
                follow_redirects=False,
            )
            assert r.status_code == 303, r.text

            # ---------- Export bundle → grab invite tokens ----------
            r = await c.get("/admin/api/export")
            assert r.status_code == 200
            bundle = r.json()
            personal_tokens = [
                link["token"] for link in bundle["links"] if link["link_type"] == "personal"
            ]
            group_tokens = [
                link["token"] for link in bundle["links"] if link["link_type"] == "group"
            ]
            assert personal_tokens and group_tokens

            # Export is ordered by ID; use the links this scenario just created.
            pt = personal_tokens[-1]
            gt = group_tokens[-1]

            # ---------- Browser test client ----------
            r = await c.get(f"/client?invite={pt}")
            assert r.status_code == 200
            assert "Sideword test client" in r.text
            assert f'value="{pt}"' in r.text
            assert r.headers["cache-control"] == "no-store"
            assert "default-src 'none'" in r.headers["content-security-policy"]
            assert r.headers["x-frame-options"] == "DENY"
            assert r.text.index('/static/client-protocol.js') < r.text.index('/static/client.js')

            r = await c.get("/static/client-protocol.js")
            assert r.status_code == 200
            assert "X25519-2DH-HKDF-SHA256-AES256GCM" in r.text

            r = await c.get("/static/client.js")
            assert r.status_code == 200
            assert "SidewordProtocol.encryptForRecipient" in r.text
            assert "persistHistoryEntry" in r.text
            assert "compareHistoryEntries" in r.text
            assert 'type: "auth", token: identity.token' in r.text

            r = await c.get(f"/l/{pt}")
            assert r.status_code == 200
            assert f"/client?invite={pt}" in r.text

            # ---------- Two clients activate the personal link ----------
            alice_pub, _ = _random_keypair()
            bob_pub, _ = _random_keypair()

            r = await c.post(
                "/api/v1/links/activate",
                json={
                    "token": pt,
                    "public_key": base64.b64encode(alice_pub).decode(),
                    "display_name": "Alice",
                },
            )
            assert r.status_code == 200, r.text
            alice = r.json()
            assert alice["chat"]["chat_type"] == "personal"
            alice_token = alice["token"]

            r = await c.post(
                "/api/v1/links/activate",
                json={
                    "token": pt,
                    "public_key": base64.b64encode(bob_pub).decode(),
                    "display_name": "Bob",
                },
            )
            assert r.status_code == 200, r.text
            bob = r.json()
            bob_token = bob["token"]

            # Third activation must fail once quota is exhausted.
            r = await c.post(
                "/api/v1/links/activate",
                json={"token": pt,
                      "public_key": base64.b64encode(secrets.token_bytes(32)).decode()},
            )
            assert r.status_code in (410, 404)

            chat_id = alice["chat"]["id"]
            alice_pid = alice["user"]["public_id"]
            bob_pid = bob["user"]["public_id"]

            # ---------- Alice → Bob ciphertext ----------
            ciphertext = base64.b64encode(b"<opaque-ciphertext-for-bob>").decode()
            r = await c.post(
                f"/api/v1/chats/{chat_id}/messages",
                json={
                    "client_message_id": "m-1",
                    "envelopes": [
                        {"recipient_public_id": bob_pid, "ciphertext": ciphertext}
                    ],
                },
                headers={"Authorization": f"Bearer {alice_token}"},
            )
            assert r.status_code == 200, r.text
            assert r.json()["recipients"] == [bob_pid]

            # ---------- Bob polls inbound ciphertext ----------
            r = await c.get(
                "/api/v1/poll",
                headers={"Authorization": f"Bearer {bob_token}"},
            )
            assert r.status_code == 200, r.text
            pending = r.json()
            assert len(pending["messages"]) == 1
            msg = pending["messages"][0]
            assert msg["sender_public_id"] == alice_pid
            assert msg["ciphertext"] == ciphertext
            assert pending["read_receipts"] == []

            # ---------- Bob marks read ----------
            r = await c.post(
                f"/api/v1/chats/{chat_id}/read",
                json={"client_message_ids": ["m-1"]},
                headers={"Authorization": f"Bearer {bob_token}"},
            )
            assert r.status_code == 200, r.text
            assert r.json()["marked"] == 1

            # Server deletes ciphertext after read.
            r = await c.get(
                "/api/v1/poll",
                headers={"Authorization": f"Bearer {bob_token}"},
            )
            assert r.status_code == 200
            assert r.json()["messages"] == []

            # Alice receives the read receipt.
            r = await c.get(
                "/api/v1/poll",
                headers={"Authorization": f"Bearer {alice_token}"},
            )
            assert r.status_code == 200
            alice_pending = r.json()
            assert len(alice_pending["read_receipts"]) == 1
            receipt = alice_pending["read_receipts"][0]
            assert receipt["reader_public_id"] == bob_pid
            assert receipt["client_message_id"] == "m-1"

            # Alice ACKs → receipt row disappears server-side.
            r = await c.post(
                "/api/v1/ack",
                json={"read_ids": [receipt["id"]]},
                headers={"Authorization": f"Bearer {alice_token}"},
            )
            assert r.status_code == 200
            assert r.json()["deleted_receipts"] == 1

            # ---------- Group chat with three members ----------
            pubs = [secrets.token_bytes(32) for _ in range(3)]
            tokens: list[str] = []
            pids: list[str] = []
            for i, pk in enumerate(pubs):
                r = await c.post(
                    "/api/v1/links/activate",
                    json={
                        "token": gt,
                        "public_key": base64.b64encode(pk).decode(),
                        "display_name": f"User{i}",
                    },
                )
                assert r.status_code == 200, r.text
                data = r.json()
                tokens.append(data["token"])
                pids.append(data["user"]["public_id"])

            group_chat_id = data["chat"]["id"]
            u0_token = tokens[0]
            others = pids[1:]

            r = await c.post(
                f"/api/v1/chats/{group_chat_id}/messages",
                json={
                    "client_message_id": "g-1",
                    "envelopes": [
                        {
                            "recipient_public_id": pid,
                            "ciphertext": base64.b64encode(
                                f"cipher-for-{pid}".encode()
                            ).decode(),
                        }
                        for pid in others
                    ],
                },
                headers={"Authorization": f"Bearer {u0_token}"},
            )
            assert r.status_code == 200, r.text
            assert set(r.json()["recipients"]) == set(others)

            # Both recipients observe the broadcast ciphertext.
            for i in (1, 2):
                r = await c.get(
                    "/api/v1/poll",
                    headers={"Authorization": f"Bearer {tokens[i]}"},
                )
                assert r.status_code == 200
                msgs = r.json()["messages"]
                assert len(msgs) == 1
                assert msgs[0]["chat_id"] == group_chat_id

            # ---------- /api/v1/me reflects chats ----------
            r = await c.get(
                "/api/v1/me",
                headers={"Authorization": f"Bearer {alice_token}"},
            )
            assert r.status_code == 200
            me = r.json()
            assert me["user"]["public_id"] == alice_pid
            assert len(me["chats"]) == 1

            # ---------- Export / import ----------
            r = await c.get("/admin/api/export")
            assert r.status_code == 200
            exported = r.json()
            assert exported["version"] == 1
            assert len(exported["users"]) >= 5
            assert len(exported["chats"]) >= 2

            payload = json.dumps(exported).encode("utf-8")
            r = await c.post(
                "/admin/api/import?replace=true",
                files={"bundle": ("b.json", payload, "application/json")},
            )
            assert r.status_code == 200, r.text
            imported = r.json()
            assert imported["imported_users"] == len(exported["users"])

            # Admin rows survive replace-import → cookie still works.
            r = await c.get("/admin/users")
            assert r.status_code == 200

            # ---------- Unknown invite token ----------
            r = await c.post(
                "/api/v1/links/activate",
                json={"token": "no-such-token",
                      "public_key": base64.b64encode(secrets.token_bytes(32)).decode()},
            )
            assert r.status_code == 404

            print("smoke OK")


def test_smoke() -> None:
    """Expose the end-to-end scenario to pytest."""

    asyncio.run(_run())


if __name__ == "__main__":
    test_smoke()
