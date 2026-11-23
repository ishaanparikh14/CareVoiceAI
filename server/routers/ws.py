"""
routers/ws.py — WebSocket endpoint for real-time alert push to nurse devices.

Endpoint
--------
GET /ws/nurse?nurse_id=<id>

How it works
------------
1.  A nurse client (phone app or browser dashboard) connects with a query-param
    nurse_id for identification in logs.
2.  The connection is registered in the ConnectionManager.
3.  Whenever POST /audio/ingest creates a new alert, the audio router calls
    ConnectionManager.broadcast() which serialises the WsAlertPayload and sends
    it to every active connection concurrently.
4.  The server sends periodic keepalive pings so mobile OS power managers do not
    kill idle sockets.  Clients that miss two consecutive pings are disconnected.
5.  On disconnect the connection is removed from the active set automatically.

Integration with audio router
------------------------------
main.py calls  routers.audio.set_broadcaster(manager.broadcast)  at startup,
injecting the broadcast coroutine without creating a circular import.
"""

import asyncio
import logging
from typing import Set

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from config import settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["WebSocket"])


# ── Connection manager ────────────────────────────────────────────────────────

class ConnectionManager:
    """
    Tracks all active nurse WebSocket connections and provides a thread-safe
    broadcast method.

    Concurrency note: FastAPI runs in a single-threaded async event loop, so a
    plain Python set is safe without locks.  If you move to a multi-process
    Uvicorn deployment you will need to replace this with a Redis pub/sub or
    similar shared-memory mechanism.
    """

    def __init__(self) -> None:
        self._connections: Set[WebSocket] = set()
        # username -> set of that nurse's live sockets (a nurse may have the
        # phone and a dashboard open at once). Enables targeted delivery.
        self._by_nurse: dict[str, Set[WebSocket]] = {}
        self._nurse_of: dict[WebSocket, str] = {}
        # Admin dashboard sockets (so dispatch escalations can be pushed to the
        # admin in real time). Admins also receive broadcasts for monitoring.
        self._admins: Set[WebSocket] = set()
        # Department-staff sockets (canteen/pharmacy/floor managers), keyed by
        # username, so department_dispatch can push a request to one person.
        self._by_dept_user: dict[str, Set[WebSocket]] = {}
        self._dept_user_of: dict[WebSocket, str] = {}
        # Callback invoked (nurse_username, online) whenever a nurse's presence
        # changes, so dispatch can react to a logout/disconnect. Injected by
        # main.py to avoid a circular import.
        self._presence_cb = None

    def set_presence_callback(self, cb) -> None:
        self._presence_cb = cb

    def _fire_presence(self, nurse: str, online: bool) -> None:
        if self._presence_cb is None or not nurse:
            return
        try:
            self._presence_cb(nurse, online)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Presence callback failed for %s: %s", nurse, exc)

    async def connect(self, ws: WebSocket, nurse: str | None = None, is_admin: bool = False) -> None:
        await ws.accept()
        self._connections.add(ws)
        if nurse:
            was_online = bool(self._by_nurse.get(nurse))
            self._by_nurse.setdefault(nurse, set()).add(ws)
            self._nurse_of[ws] = nurse
            if not was_online:
                self._fire_presence(nurse, True)
        if is_admin:
            self._admins.add(ws)
        logger.info(
            "Nurse WebSocket connected (nurse=%s admin=%s). Active connections: %d",
            nurse or "?", is_admin, len(self._connections),
        )

    async def connect_department(self, ws: WebSocket, username: str) -> None:
        """Register a department-staff socket (canteen/pharmacy/floor manager).

        NOTE: the /ws/department route already called ws.accept() (so it can
        reject a bad token with a close code before we get here), so we must NOT
        accept again — a double accept raises and crashes the socket."""
        self._connections.add(ws)
        self._by_dept_user.setdefault(username, set()).add(ws)
        self._dept_user_of[ws] = username
        logger.info("Department WS connected (user=%s). Active connections: %d",
                    username, len(self._connections))

    def is_dept_user_online(self, username: str) -> bool:
        return bool(self._by_dept_user.get(username))

    async def send_to_dept_user(self, username: str, message: str) -> bool:
        """Deliver to all of one department user's sockets. True if any succeeded."""
        socks = list(self._by_dept_user.get(username, set()))
        if not socks:
            return False
        results = await asyncio.gather(
            *[ws.send_text(message) for ws in socks], return_exceptions=True
        )
        ok = False
        for ws, result in zip(socks, results):
            if isinstance(result, Exception):
                self.disconnect(ws)
            else:
                ok = True
        return ok

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.discard(ws)
        self._admins.discard(ws)
        dept_user = self._dept_user_of.pop(ws, None)
        if dept_user:
            socks = self._by_dept_user.get(dept_user)
            if socks:
                socks.discard(ws)
                if not socks:
                    self._by_dept_user.pop(dept_user, None)
        nurse = self._nurse_of.pop(ws, None)
        if nurse:
            socks = self._by_nurse.get(nurse)
            if socks:
                socks.discard(ws)
                if not socks:
                    self._by_nurse.pop(nurse, None)
                    # Last socket for this nurse closed → she is now OFFLINE.
                    self._fire_presence(nurse, False)
        logger.info(
            "Nurse WebSocket disconnected. Active connections: %d",
            len(self._connections),
        )

    def is_online(self, nurse: str) -> bool:
        """True if the nurse has at least one live socket."""
        return bool(self._by_nurse.get(nurse))

    @property
    def online_nurses(self) -> set[str]:
        return set(self._by_nurse.keys())

    async def send_to_admins(self, message: str) -> None:
        """Deliver a message to every connected admin dashboard socket."""
        socks = list(self._admins)
        if not socks:
            return
        results = await asyncio.gather(
            *[ws.send_text(message) for ws in socks],
            return_exceptions=True,
        )
        for ws, result in zip(socks, results):
            if isinstance(result, Exception):
                self.disconnect(ws)

    async def send_to_nurse(self, nurse: str, message: str) -> bool:
        """Deliver a message to all of one nurse's sockets. Returns True if at
        least one delivery succeeded."""
        socks = list(self._by_nurse.get(nurse, set()))
        if not socks:
            return False
        results = await asyncio.gather(
            *[ws.send_text(message) for ws in socks],
            return_exceptions=True,
        )
        ok = False
        for ws, result in zip(socks, results):
            if isinstance(result, Exception):
                self.disconnect(ws)
            else:
                ok = True
        return ok

    async def broadcast(self, message: str) -> None:
        """
        Send a JSON string to every connected nurse client concurrently.

        Stale connections (those that raise an exception during send) are
        removed from the active set so they do not accumulate indefinitely.
        """
        if not self._connections:
            return

        stale: list[WebSocket] = []

        # Fire all sends concurrently and collect results.
        results = await asyncio.gather(
            *[ws.send_text(message) for ws in self._connections],
            return_exceptions=True,
        )

        for ws, result in zip(list(self._connections), results):
            if isinstance(result, Exception):
                logger.warning("Broadcast failed for a client — removing: %s", result)
                stale.append(ws)

        for ws in stale:
            self.disconnect(ws)

    @property
    def active_count(self) -> int:
        return len(self._connections)


# Module-level singleton — main.py imports this to inject into the audio router.
manager = ConnectionManager()


def _identity_from_token(websocket: WebSocket) -> tuple[str | None, str | None]:
    """Return (username, role) from a Bearer header or ?token= query param.

    Nurse tokens are keyed for targeted delivery; admin tokens register as admin
    dashboard sockets so dispatch escalations reach them. Returns (None, None)
    if absent or invalid.
    """
    raw = None
    auth = websocket.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        raw = auth[7:].strip()
    if not raw:
        raw = websocket.query_params.get("token")
    if not raw:
        return None, None
    try:
        from auth import decode_token
        payload = decode_token(raw)
    except Exception:
        return None, None
    role = payload.get("role")
    if role not in ("nurse", "admin"):
        return None, None
    return payload.get("sub"), role


# ── WebSocket route ───────────────────────────────────────────────────────────

@router.websocket("/ws/nurse")
async def nurse_ws(
    websocket: WebSocket,
    nurse_id:  str = Query(default="unknown", description="Nurse identifier for logging"),
):
    """
    Persistent WebSocket connection for a nurse client.

    The client should:
      • Connect once and keep the socket open.
      • Parse incoming text frames as JSON (WsAlertPayload schema).
      • Respond to "ping" text frames with "pong" to keep the connection alive.
      • Reconnect with exponential back-off if disconnected.

    The server sends:
      • {"event": "new_alert", ...WsAlertPayload fields} on every new alert.
      • "ping" every WS_KEEPALIVE_SECONDS to prevent idle disconnection.
      • {"event": "connected", "nurse_id": "..."} immediately on connect.
    """
    # Identify the nurse from their JWT (Authorization header or ?token=) so
    # targeted routing can address this socket by username. The Android app
    # sends the header; the web dashboard may pass ?token=. Fall back to the
    # legacy nurse_id query param only if no valid token is present.
    username, role = _identity_from_token(websocket)
    is_admin = role == "admin"
    nurse_username = username if role == "nurse" else None
    if nurse_username is None and not is_admin and nurse_id != "unknown":
        nurse_username = nurse_id
    await manager.connect(websocket, nurse=nurse_username, is_admin=is_admin)
    nurse_id = username or nurse_id
    logger.info("Nurse WebSocket '%s' connected (role=%s)", nurse_id, role or "?")

    # Send an immediate welcome frame so the client knows the connection is live.
    await websocket.send_json({"event": "connected", "nurse_id": nurse_id})

    # Backlog: deliver any orders currently awaiting approval so a nurse who was
    # offline when the canteen/pharmacy requested approval still sees them on
    # (re)connect. Without this, an approval request pushed while the nurse's
    # socket was down would be lost (the bug where "nurse didn't receive the
    # request after the canteen recommended the meal").
    if nurse_username or is_admin:
        try:
            import aiosqlite
            from database import list_orders_pending_approval
            async with aiosqlite.connect(settings.DB_PATH) as _db:
                _db.row_factory = aiosqlite.Row
                pend = await list_orders_pending_approval(_db)
            for r in pend:
                d = dict(r)
                await websocket.send_json({
                    "event": "order_needs_approval", "request_id": d["id"], "alert_id": d["alert_id"],
                    "department": d["department"], "kind": d.get("kind"), "room_id": d["room_id"],
                    "transcript": d["transcript"], "order_status": d.get("order_status"),
                    "item_name": d.get("item_name"), "item_detail": d.get("item_detail"),
                    "diet_restriction": d.get("diet_restriction"), "backlog": True,
                })
        except Exception as exc:
            logger.warning("Nurse approval backlog delivery failed for %s: %s", nurse_id, exc)

    try:
        while True:
            # Wait for either an incoming client frame OR a keepalive timeout.
            try:
                # timeout = WS_KEEPALIVE_SECONDS so we can send a ping if idle.
                message = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=settings.WS_KEEPALIVE_SECONDS,
                )

                if message == "pong":
                    # Client responded to our ping — connection is healthy.
                    logger.debug("Pong received from nurse '%s'", nurse_id)
                else:
                    # Unexpected client message — log and ignore.
                    logger.debug(
                        "Unexpected message from nurse '%s': %s", nurse_id, message
                    )

            except asyncio.TimeoutError:
                # Keepalive interval elapsed — send a ping.
                # If the client does not respond, the next receive will raise
                # WebSocketDisconnect or another exception and we clean up below.
                logger.debug("Sending keepalive ping to nurse '%s'", nurse_id)
                await websocket.send_text("ping")

    except WebSocketDisconnect:
        logger.info("Nurse '%s' disconnected (WebSocketDisconnect)", nurse_id)
    except Exception as exc:
        logger.warning("Nurse '%s' WebSocket error: %s", nurse_id, exc)
    finally:
        manager.disconnect(websocket)


# ── Department WebSocket route ────────────────────────────────────────────────

@router.websocket("/ws/department")
async def department_ws(
    websocket: WebSocket,
    token: str = Query(default="", description="JWT access token (department-staff account)"),
):
    """Persistent socket for a department dashboard (canteen/pharmacy/floor mgr).

    The server pushes `department_request` frames to the resolved assignee and
    `department_cancelled` frames when a request is taken/escalated elsewhere.
    Auth: the JWT role must be one of the department-staff roles.
    """
    await websocket.accept()
    raw = token or websocket.query_params.get("token", "")
    try:
        from auth import decode_token, DEPARTMENT_ROLES
        payload = decode_token(raw)
        username = payload.get("sub")
        role = payload.get("role")
    except Exception:
        await websocket.send_json({"event": "error", "message": "Invalid or expired token"})
        await websocket.close(code=4001)
        return
    if role not in DEPARTMENT_ROLES or not username:
        await websocket.send_json({"event": "error", "message": "Department-staff role required"})
        await websocket.close(code=4003)
        return

    await manager.connect_department(websocket, username)
    await websocket.send_json({"event": "connected", "username": username, "role": role})

    # Deliver any requests already pending for this user (offline catch-up).
    # Guarded so a single bad row can never close the socket.
    try:
        import aiosqlite
        from database import list_department_requests_for_user
        async with aiosqlite.connect(settings.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            pending = await list_department_requests_for_user(db, username)
        for r in pending:
            d = dict(r)
            await websocket.send_json({
                "event": "order_update" if d.get("kind") in ("food", "medicine") else "department_request",
                "request_id": d["id"], "alert_id": d["alert_id"],
                "department": d["department"], "kind": d.get("kind", "facility"),
                "room_id": d["room_id"], "floor": d.get("floor"),
                "transcript": d["transcript"], "created_at": d["created_at"],
                "state": d.get("state"), "order_status": d.get("order_status", "requested"),
                "item_name": d.get("item_name"), "item_detail": d.get("item_detail"),
                "needs_approval": bool(d.get("needs_approval")),
                "diet_restriction": d.get("diet_restriction"),
                "repeat_count": d.get("repeat_count", 0), "backlog": True,
            })
    except Exception as exc:
        logger.warning("Department backlog delivery failed for %s: %s", username, exc)

    try:
        while True:
            try:
                message = await asyncio.wait_for(
                    websocket.receive_text(), timeout=settings.WS_KEEPALIVE_SECONDS
                )
                if message != "pong":
                    logger.debug("Department WS '%s' msg: %s", username, message)
            except asyncio.TimeoutError:
                await websocket.send_text("ping")
    except WebSocketDisconnect:
        logger.info("Department user '%s' disconnected", username)
    except Exception as exc:
        logger.warning("Department WS '%s' error: %s", username, exc)
    finally:
        manager.disconnect(websocket)
