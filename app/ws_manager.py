"""Tracks active WebSocket connections per user."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy import select

from .config import get_settings
from .db import SessionLocal
from .models import ChatMember, User

HEARTBEAT_TIMEOUT_SECONDS = 75


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[int, set[WebSocket]] = defaultdict(set)
        self._validators: dict[WebSocket, Callable[[], Awaitable[bool]]] = {}
        self._lock = asyncio.Lock()
        self._activity: dict[WebSocket, float] = {}
        self._public_ids: dict[WebSocket, str] = {}
        self._presence: set[WebSocket] = set()
        self._presence_lock = asyncio.Lock()

    async def connect(
        self, user_id: int, websocket: WebSocket, validate: Callable[[], Awaitable[bool]],
        *, public_id: str,
    ) -> None:
        async with self._lock:
            if len(self._connections[user_id]) >= get_settings().max_ws_per_user:
                await websocket.close(code=1008)
                raise WebSocketDisconnect(code=1008)
            self._connections[user_id].add(websocket)
            self._validators[websocket] = validate
            self._public_ids[websocket] = public_id
            self.touch(websocket)

    async def disconnect(
        self, user_id: int, websocket: WebSocket, *, notify: bool = True
    ) -> None:
        async with self._lock:
            registered = websocket in self._validators
            self._validators.pop(websocket, None)
            self._activity.pop(websocket, None)
            self._public_ids.pop(websocket, None)
            self._presence.discard(websocket)
            sockets = self._connections.get(user_id)
            if sockets is None:
                return
            sockets.discard(websocket)
            if not sockets:
                self._connections.pop(user_id, None)
        if registered and notify:
            await self.refresh_presence()

    def touch(self, websocket: WebSocket) -> None:
        self._activity[websocket] = time.monotonic()

    def remaining(self, websocket: WebSocket) -> float:
        if websocket not in self._activity:
            return 0
        # Legacy sockets retain their existing transport-level ping/pong policy.
        # Only clients opting into presence acquire the application heartbeat.
        if websocket not in self._presence:
            return float("inf")
        return max(0, HEARTBEAT_TIMEOUT_SECONDS - (
            time.monotonic() - self._activity[websocket]
        ))

    def is_online(self, user_id: int) -> bool:
        return bool(self._connections.get(user_id))

    async def validate(
        self, user_id: int, websocket: WebSocket, *, notify: bool = True
    ) -> bool:
        """Check this socket's credential, not another session for the same user."""
        validate = self._validators.get(websocket)
        if validate is None:
            return False
        if await validate():
            return True
        try:
            await websocket.send_json({"type": "auth_error", "reason": "invalid session"})
            await websocket.close(code=1008)
        finally:
            await self.disconnect(user_id, websocket, notify=notify)
        return False

    async def enable_presence(self, websocket: WebSocket) -> None:
        if get_settings().presence_enabled and websocket in self._validators:
            self._presence.add(websocket)
        await self.refresh_presence()

    async def refresh_presence(self, websocket: WebSocket | None = None) -> None:
        """Send short-lived, complete snapshots scoped to current DB memberships."""
        if not self._presence:
            return
        send_removed_socket = False
        async with self._presence_lock:
            # Recheck each credential independently, including peers whose session
            # may have been invalidated without a local revoke notification.
            for user_id, sockets in list(self._connections.items()):
                for ws in list(sockets):
                    try:
                        if not self.remaining(ws):
                            await asyncio.wait_for(ws.close(code=1001), timeout=5)
                            await self.disconnect(user_id, ws, notify=False)
                            websocket = None
                        elif websocket is None:
                            await asyncio.wait_for(
                                self.validate(user_id, ws, notify=False), timeout=5
                            )
                    except Exception:
                        await self.disconnect(user_id, ws, notify=False)
            observers = {
                user_id: [
                    ws for ws in sockets if ws in self._presence
                    and (websocket is None or websocket is ws)
                ]
                for user_id, sockets in self._connections.items()
            }
            observers = {uid: sockets for uid, sockets in observers.items() if sockets}
            if not observers:
                return
            async with SessionLocal() as session:
                memberships = (await session.execute(
                    select(ChatMember.user_id, ChatMember.chat_id)
                    .where(ChatMember.user_id.in_(observers))
                )).all()
                chat_ids = {chat_id for _, chat_id in memberships}
                peers = (await session.execute(
                    select(ChatMember.chat_id, User.id, User.public_id)
                    .join(User, User.id == ChatMember.user_id)
                    .where(ChatMember.chat_id.in_(chat_ids))
                )).all()
            for user_id, sockets in observers.items():
                allowed = {cid for uid, cid in memberships if uid == user_id}
                snapshots = []
                lifetime = 35.0
                for chat_id in sorted(allowed):
                    online = []
                    for cid, uid, public_id in peers:
                        if cid != chat_id:
                            continue
                        remaining = max(
                            (self.remaining(ws) for ws in self._connections.get(uid, ())
                             if self._public_ids.get(ws) == public_id),
                            default=0,
                        )
                        if remaining > 0:
                            online.append(public_id)
                            lifetime = min(lifetime, remaining)
                    snapshots.append({
                        "chat_id": chat_id,
                        "online": sorted(online),
                        "participants": sorted(pid for cid, _, pid in peers if cid == chat_id),
                    })
                deadline = time.monotonic() + lifetime
                for ws in sockets:
                    try:
                        if await asyncio.wait_for(
                            self.validate(user_id, ws, notify=False), timeout=5
                        ):
                            payload = {
                                "type": "presence", "chats": snapshots,
                                "valid_for_ms": max(0, int((deadline - time.monotonic()) * 1000)),
                            }
                            await asyncio.wait_for(ws.send_json(payload), timeout=5)
                        else:
                            send_removed_socket = True
                    except Exception:
                        await self.disconnect(user_id, ws, notify=False)
                        send_removed_socket = True
        if send_removed_socket:
            await self.refresh_presence()

    async def send_json(self, user_id: int, payload: dict[str, Any]) -> None:
        sockets = list(self._connections.get(user_id, ()))
        for ws in sockets:
            try:
                if await self.validate(user_id, ws):
                    await asyncio.wait_for(ws.send_json(payload), timeout=5)
            except Exception:
                await self.disconnect(user_id, ws)

    async def revoke(self, user_ids: set[int]) -> None:
        """Notify and close every local socket for revoked sessions."""

        for user_id in user_ids:
            sockets = list(self._connections.get(user_id, ()))
            for ws in sockets:
                try:
                    await ws.send_json(
                        {"type": "auth_error", "reason": "invite revoked"}
                    )
                    await ws.close(code=1008)
                except Exception:
                    pass
                finally:
                    await self.disconnect(user_id, ws)


manager = ConnectionManager()
