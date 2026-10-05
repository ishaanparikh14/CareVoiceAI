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

    async def connect(self, ws: WebSocket, nurse: str | None = None) -> None:
        await ws.accept()
        self._connections.add(ws)
        if nurse:
            self._by_nurse.setdefault(nurse, set()).add(ws)
            self._nurse_of[ws] = nurse
        logger.info(
            "Nurse WebSocket connected (nurse=%s). Active connections: %d",
            nurse or "?", len(self._connections),
        )

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.discard(ws)
        nurse = self._nurse_of.pop(ws, None)
        if nurse:
            socks = self._by_nurse.get(nurse)
            if socks:
                socks.discard(ws)
                if not socks:
                    self._by_nurse.pop(nurse, None)
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
    await manager.connect(websocket, nurse=nurse_id if nurse_id != "unknown" else None)
    logger.info("Nurse '%s' connected via WebSocket", nurse_id)

    # Send an immediate welcome frame so the client knows the connection is live.
    await websocket.send_json({"event": "connected", "nurse_id": nurse_id})

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
