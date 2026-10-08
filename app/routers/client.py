"""Lightweight browser client for local and interoperability testing."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..templates import templates

router = APIRouter(tags=["test-client"])

_CLIENT_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'; object-src 'none'"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


@router.get("/client", response_class=HTMLResponse)
async def test_client(request: Request, invite: str = "") -> HTMLResponse:
    """Serve the dependency-free test client with a locked-down policy."""

    safe_invite = invite if len(invite) <= 128 else ""
    return templates.TemplateResponse(
        request,
        "client.html",
        {"invite_token": safe_invite},
        headers=_CLIENT_HEADERS,
    )
