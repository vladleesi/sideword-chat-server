"""Run local administration and a restricted public listener in one process."""

import asyncio

import uvicorn
from starlette.responses import PlainTextResponse

from app.config import get_settings
from app.main import create_app


class PublicListener:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        allowed = (
            path in ("/client", "/ws", "/health")
            or path.startswith(("/api/v1/", "/l/", "/static/"))
        )
        if not allowed:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await PlainTextResponse("Not found", status_code=404)(scope, receive, send)
            return
        await self.app(scope, receive, send)


async def main():
    app = create_app()
    # Invite paths contain credentials.
    # Uvicorn also logs WS URLs at INFO on uvicorn.error, independently of its
    # access_log option. Preserve warnings/errors and application error logging.
    local = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=8000, access_log=False, log_level="warning", ws_max_size=4096,
        limit_concurrency=256, forwarded_allow_ips=get_settings().trusted_proxy_ips,
    ))
    public = uvicorn.Server(uvicorn.Config(
        PublicListener(app), host="127.0.0.1", port=8001, lifespan="off",
        access_log=False, log_level="warning", ws_max_size=4096,
        limit_concurrency=256, forwarded_allow_ips=get_settings().trusted_proxy_ips,
    ))
    await asyncio.gather(local.serve(), public.serve())


if __name__ == "__main__":
    asyncio.run(main())
