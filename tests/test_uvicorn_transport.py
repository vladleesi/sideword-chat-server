"""Exercise the real HTTP/WebSocket transport against a private test database."""

import base64
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import httpx2
import pytest
from message_key_fixtures import public_key
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

SERVER = """
import asyncio
import os
import socket
import sys
from pathlib import Path

import uvicorn

from app.db import init_db, session_scope
from app.main import create_app
from app.models import Link, LinkType
from scripts.serve_shared import PublicListener

async def seed():
    await init_db()
    async with session_scope() as session:
        session.add(Link(token=os.environ['SIDEWORD_TEST_INVITE'],
                         link_type=LinkType.personal, max_uses=2, uses_count=0,
                         is_active=True))
        await session.commit()

asyncio.run(seed())
with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
    listener.listen(128)
    port_path = Path(sys.argv[1])
    pending_path = port_path.with_suffix('.pending')
    pending_path.write_text(str(listener.getsockname()[1]))
    pending_path.replace(port_path)
    config = uvicorn.Config(PublicListener(create_app()), access_log=False,
                            log_level='critical', ws_max_size=4096,
                            forwarded_allow_ips='127.0.0.1')
    uvicorn.Server(config).run(sockets=[listener])
"""


def test_real_uvicorn_http_websocket_auth_and_public_isolation(tmp_path):
    invite = secrets.token_urlsafe(32)
    env = {
        **os.environ,
        "SIDEWORD_DB_PATH": str(tmp_path / "transport.sqlite3"),
        "SIDEWORD_EXPORTS_DIR": str(tmp_path / "exports"),
        "SIDEWORD_SECRET_KEY": secrets.token_urlsafe(32),
        "SIDEWORD_ADMIN_USERNAME": "",
        "SIDEWORD_ADMIN_PASSWORD": "",
        "SIDEWORD_TEST_INVITE": invite,
        "SIDEWORD_REQUIRE_HTTPS": "false",
    }
    port_file = tmp_path / "port"
    with (tmp_path / "server-output").open("wb") as output:
        process = subprocess.Popen(
            [sys.executable, "-c", SERVER, str(port_file)], env=env,
            cwd=Path(__file__).resolve().parents[1], stdout=output, stderr=output,
        )
        try:
            deadline = time.monotonic() + 20
            with httpx2.Client(timeout=2, trust_env=False) as client:
                while True:
                    assert process.poll() is None, "Isolated transport server failed to start"
                    if port_file.exists():
                        port = int(port_file.read_text())
                        base = f"http://127.0.0.1:{port}"
                        try:
                            if client.get(base + "/health").status_code == 200:
                                break
                        except httpx2.TransportError:
                            pass
                    assert time.monotonic() < deadline, "Isolated transport startup timed out"
                    time.sleep(0.05)
                assert client.get(base + "/admin/login").status_code == 404
                response = client.post(base + "/api/v1/links/activate", json={
                    "token": invite,
                    "public_key": base64.b64encode(public_key()).decode(),
                    "session_credential": secrets.token_urlsafe(32),
                })
                assert response.status_code == 200
                identity = response.json()
                socket_base = f"ws://127.0.0.1:{port}"
                with connect(socket_base + "/ws", subprotocols=["sideword.v1"]) as websocket:
                    websocket.send(json.dumps({"type": "auth", "token": identity["token"]}))
                    hello = json.loads(websocket.recv(timeout=3))
                    assert hello["type"] == "hello"
                    assert hello["user"] == identity["user"]["public_id"]
                with connect(socket_base + "/ws", subprotocols=["sideword.v1"]) as websocket:
                    websocket.send(json.dumps({"type": "auth", "token": "invalid"}))
                    assert json.loads(websocket.recv(timeout=3))["type"] == "auth_error"
                    with pytest.raises(ConnectionClosed) as closed:
                        websocket.recv(timeout=3)
                    assert closed.value.rcvd.code == 1008
                with pytest.raises(InvalidStatus) as denied:
                    with connect(socket_base + "/admin/login"):
                        pytest.fail("Public administration WebSocket must be rejected")
                assert denied.value.response.status_code == 403
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
