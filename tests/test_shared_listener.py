import pytest
from starlette.responses import PlainTextResponse
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from scripts.serve_shared import PublicListener


async def origin(scope, receive, send):
    await PlainTextResponse("origin reached")(scope, receive, send)


@pytest.mark.parametrize("path", ["/admin/login", "/admin/api/export", "/docs", "/openapi.json",
                                  "/admin/chats/7/close", "/admin/chats/7/reopen"])
def test_public_listener_blocks_private_http_routes(path):
    response = TestClient(PublicListener(origin)).get(path)
    assert response.status_code == 404
    assert "origin reached" not in response.text


@pytest.mark.parametrize(
    "path", ["/client", "/health", "/api/v1/me", "/l/test", "/static/client.js"]
)
def test_public_listener_forwards_client_routes(path):
    response = TestClient(PublicListener(origin)).get(path)
    assert response.status_code == 200
    assert response.text == "origin reached"


def test_public_listener_rejects_private_websocket_path():
    with pytest.raises(WebSocketDisconnect) as error:
        with TestClient(PublicListener(origin)).websocket_connect("/admin"):
            pytest.fail("Private WebSocket path was accepted")
    assert error.value.code == 1008


def test_shared_runner_suppresses_http_and_websocket_access_logs(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from scripts import serve_shared

    configurations = []

    class Server:
        def __init__(self, config):
            configurations.append(config)

        async def serve(self):
            pass

    monkeypatch.setattr(serve_shared.uvicorn, "Server", Server)
    monkeypatch.setattr(
        serve_shared.uvicorn, "Config", lambda app, **kwargs: SimpleNamespace(**kwargs)
    )
    asyncio.run(serve_shared.main())
    assert len(configurations) == 2
    for config in configurations:
        assert config.access_log is False
        # Uvicorn logs WS URLs on uvicorn.error at INFO, independent of access_log.
        assert config.log_level == "warning"
