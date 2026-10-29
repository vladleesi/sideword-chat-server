"""Bounded ASGI requests, transport checks and cookie-bound admin CSRF."""

import asyncio
import hashlib
import hmac
import ipaddress
import re
import secrets
import time
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.websockets import WebSocketDisconnect

from .config import get_settings

CSRF_COOKIE = "sideword_csrf"
ADMIN_COOKIE = "sideword_admin_session"


def local_transport(scope):
    try:
        raw_host = dict(scope.get("headers", [])).get(b"host", b"").decode("latin-1")
        host = urlsplit("//" + raw_host).hostname
        peer_local = ipaddress.ip_address((scope.get("client") or ("", 0))[0]).is_loopback
    except ValueError:
        return False
    return peer_local and host in {"localhost", "127.0.0.1", "::1"}


def csrf_token(binding, nonce=None):
    nonce = nonce or secrets.token_hex(24)
    signature = hmac.new(get_settings().secret_key.encode(),
                         (nonce + "|" + binding).encode(), hashlib.sha256).hexdigest()
    return nonce + "." + signature


def valid_csrf(token, binding):
    return isinstance(token, str) and token.isascii() and len(token) == 113 and hmac.compare_digest(
        token, csrf_token(binding, token.split(".")[0])
    )


class SecurityGuards:
    def __init__(self, app):
        self.app = app
        self.rates = {}
        self.connections = 0
        self.http_requests = 0
        self.static_requests = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        settings = get_settings()
        websocket = scope["type"] == "websocket"

        async def reject(status, detail):
            if websocket:
                await send({"type": "websocket.close", "code": 1008})
            else:
                await JSONResponse({"detail": detail}, status_code=status,
                                   headers={"Cache-Control": "no-store",
                                            "Referrer-Policy": "no-referrer"})(scope, receive, send)

        private = (scope["path"] == "/admin" or scope["path"].startswith("/admin/")
                   or scope["path"] in {"/docs", "/redoc", "/openapi.json"})
        if private and settings.private_admin and not local_transport(scope):
            return await reject(404, "not found")
        if (settings.require_https and scope["scheme"] not in {"https", "wss"}
                and not local_transport(scope)):
            return await reject(403, "HTTPS/WSS is required outside loopback")
        now = time.monotonic()
        peer = (scope.get("client") or ("unknown", 0))[0]
        # Per-process rate state is bounded and never contains request URLs or tokens.
        self.rates = {key: value for key, value in self.rates.items() if value[0] > now}
        if peer not in self.rates and len(self.rates) >= 10000:
            return await reject(429, "request capacity reached")
        deadline, count = self.rates.get(peer, (now + 60, 0))
        if count >= settings.requests_per_minute:
            return await reject(429, "request rate exceeded")
        self.rates[peer] = (deadline, count + 1)
        if websocket:
            if self.connections >= settings.max_ws_connections:
                return await reject(429, "connection capacity reached")
            self.connections += 1
            frames = []
            closed = False

            async def bounded_send(message):
                nonlocal closed
                if closed:
                    return
                if message["type"] == "websocket.close":
                    closed = True
                if message["type"] == "websocket.send":
                    size = len(message.get("bytes") or (message.get("text") or "").encode())
                    if size > 4 * 1024 * 1024:
                        closed = True
                        await send({"type": "websocket.close", "code": 1009})
                        raise WebSocketDisconnect(1009)
                    try:
                        await asyncio.wait_for(send(message), timeout=5)
                    except TimeoutError as exc:
                        closed = True
                        raise WebSocketDisconnect(1001) from exc
                else:
                    await send(message)

            async def bounded_receive():
                message = await receive()
                if message["type"] == "websocket.receive":
                    size = len(message.get("bytes") or (message.get("text") or "").encode())
                    timestamp = time.monotonic()
                    frames[:] = [t for t in frames if t > timestamp - 60]
                    if size > 4096 or len(frames) >= 120:
                        await bounded_send({"type": "websocket.close", "code": 1009})
                        return {"type": "websocket.disconnect", "code": 1009}
                    frames.append(timestamp)
                return message
            try:
                return await self.app(scope, bounded_receive, bounded_send)
            finally:
                self.connections -= 1

        # Script/font loads must not compete with expensive message requests.
        static_read = scope["method"] in {"GET", "HEAD"} and scope["path"].startswith("/static/")
        counter = "static_requests" if static_read else "http_requests"
        limit = 32 if static_read else settings.max_http_concurrency
        if getattr(self, counter) >= limit:
            return await reject(429, "request concurrency capacity reached; retry later")
        setattr(self, counter, getattr(self, counter) + 1)
        try:
            return await self.http(scope, receive, send, reject)
        finally:
            setattr(self, counter, getattr(self, counter) - 1)

    async def http(self, scope, receive, send, reject):
        settings = get_settings()
        send_request = scope["method"] == "POST" and re.fullmatch(
            r"/api/v1/chats/[0-9]+/messages", scope["path"]
        )
        limit = settings.max_send_request_bytes if send_request else settings.max_request_bytes
        request = Request(scope)
        body = bytearray()
        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            return await reject(400, "invalid content length")
        if length < 0 or length > limit:
            return await reject(413, "request too large")
        body_deadline = time.monotonic() + 15
        while True:
            try:
                message = await asyncio.wait_for(
                    receive(), max(0.001, body_deadline - time.monotonic()))
            except TimeoutError:
                return await reject(408, "request body timeout")
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > limit:
                return await reject(413, "request too large")
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        def replay():
            consumed = False

            async def read():
                nonlocal consumed
                if consumed:
                    return await receive()
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return read

        admin = scope["path"].startswith("/admin")
        binding = request.cookies.get(ADMIN_COOKIE, "")
        token = request.cookies.get(CSRF_COOKIE, "")
        safe = scope["method"] in {"GET", "HEAD", "OPTIONS"}
        bearer_only = (scope["path"].startswith("/admin/api/") and not binding
                       and request.headers.get("authorization", "").lower().startswith("bearer "))
        if admin and not safe and not bearer_only:
            supplied = request.headers.get("x-csrf-token")
            if supplied is None:
                parsed = Request(scope, replay())
                try:
                    form = await parsed.form(max_files=1, max_fields=100)
                    supplied = form.get("csrf_token")
                    await form.close()
                except Exception:
                    return await reject(403, "invalid CSRF form")
            origin = request.headers.get("origin")
            expected = urlsplit(str(request.url))
            # no-referrer form navigation can deliberately serialize Origin as
            # null. Fetch Metadata is browser-controlled; same-site is not enough.
            opaque_same_origin = (
                origin in {None, "null"}
                and request.headers.get("sec-fetch-site") == "same-origin"
            )
            if (not valid_csrf(token, binding) or not isinstance(supplied, str)
                    or not supplied.isascii()
                    or not hmac.compare_digest(token, supplied)
                    or (not origin and not opaque_same_origin
                        and not request.headers.get("x-csrf-token"))
                    or (origin and origin != f"{expected.scheme}://{expected.netloc}"
                        and not opaque_same_origin)):
                return await reject(403, "invalid CSRF token or origin")
        if admin:
            if not valid_csrf(token, binding):
                token = csrf_token(binding)
            scope.setdefault("state", {})["csrf_token"] = token

        async def guarded_send(message):
            if message["type"] == "http.response.start" and admin:
                cookie = JSONResponse({})
                cookie.set_cookie(CSRF_COOKIE, token, httponly=True, samesite="strict",
                                  secure=scope["scheme"] == "https"
                                  or settings.public_url.startswith("https://"), path="/admin")
                message["headers"] = [(k, v) for k, v in message["headers"]
                                      if k.lower() != b"cache-control"] + [
                    (b"set-cookie", cookie.headers["set-cookie"].encode()),
                    (b"cache-control", b"no-store"),
                ]
            await send(message)
        await self.app(scope, replay(), guarded_send)
