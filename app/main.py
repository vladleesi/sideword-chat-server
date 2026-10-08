"""FastAPI application entrypoint for Sideword."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from .admin_setup import ensure_default_admin
from .cleanup import run_periodic_cleanup
from .db import init_db
from .guards import SecurityGuards
from .routers import (
    admin_auth,
    admin_export,
    admin_ui,
    chats,
    client,
    health,
    landing,
    links,
    me,
    sessions,
    ws,
)
from .version import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

_STATIC_DIR = Path(__file__).parent / "static"


class PublicStaticFiles(StaticFiles):
    """Cache and buffer only bundled public assets, never application responses."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code in (200, 206, 304):
            # Asset query revisions change on updates; avoid immutable caching
            # because older/unversioned URLs can still resolve to updated files.
            response.headers["Cache-Control"] = "public, max-age=3600, must-revalidate"
            response.headers["X-Accel-Buffering"] = "yes"
            if response.status_code == 304:
                response.headers.add_vary_header("Accept-Encoding")
        return response


class StaticGZipMiddleware(GZipMiddleware):
    async def __call__(self, scope, receive, send):
        # Preserve byte ranges over the original file representation. Compressing
        # a partial body would make Content-Range describe the wrong encoding.
        if scope["type"] == "http" and any(name == b"range" for name, _ in scope["headers"]):
            await self.app(scope, receive, send)
        else:
            await super().__call__(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await ensure_default_admin()
    cleanup_task = asyncio.create_task(run_periodic_cleanup())
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass


def create_app() -> FastAPI:
    app = FastAPI(
        title="Sideword Server",
        version=__version__,
        description="Link-only messenger backend API.",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def private_responses(request: Request, call_next):
        response = await call_next(request)
        public_asset = (request.url.path.startswith("/static/")
                        and response.status_code in (200, 206, 304))
        if not public_asset:
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(RequestValidationError)
    async def private_validation_errors(request: Request, exc: RequestValidationError):
        # Pydantic's default errors echo request inputs, including credentials.
        return JSONResponse(status_code=422, content={"detail": [
            {"loc": error["loc"], "msg": error["msg"], "type": error["type"]}
            for error in exc.errors()
        ]})

    # Compress only public files: invite HTML and authenticated responses must
    # never mix credentials or user content into a compression context.
    app.mount("/static", StaticGZipMiddleware(
        PublicStaticFiles(directory=str(_STATIC_DIR)), minimum_size=1024, compresslevel=5,
    ), name="static")

    app.include_router(health.router)
    app.include_router(landing.router)
    app.include_router(links.router)
    app.include_router(me.router)
    app.include_router(sessions.router)
    app.include_router(chats.router)
    app.include_router(ws.router)
    app.include_router(client.router)
    app.include_router(admin_auth.router)
    app.include_router(admin_ui.router)
    app.include_router(admin_export.router)

    app.add_middleware(SecurityGuards)
    return app


app = create_app()
