"""
signal.py — WebRTC signaling relay for real-time patient ↔ nurse voice calls.

This router provides a single authenticated WebSocket endpoint, ``/ws/signal``,
that BOTH patients and nurses connect to and keep open for the lifetime of the
session.  It is the *signaling plane* only: it exchanges the small JSON control
messages (SDP offer/answer + trickled ICE candidates + call lifecycle events)
that two WebRTC peers need in order to find each other and negotiate a media
session.

    Microphone → WebRTC → LAN → WebRTC → Speaker

The audio itself (Opus over SRTP) flows **directly device-to-device across the
hospital LAN** and never passes through this server.  This module must never
become an audio relay — it only forwards control frames.

Design
------
* One WebSocket per logged-in user, keyed by username in ``SignalRegistry``.
* Auth reuses the existing JWT (``?token=`` query param → ``decode_token``),
  exactly like ``/ws/patient``.
* Messages are JSON objects with an ``event`` discriminator (see
  ``models.SignalMessage`` / ``SIGNAL_EVENTS``).
* The server stamps the authenticated sender identity (``from_user`` /
  ``from_name`` / ``from_role``) onto every relayed frame so a client can never
  spoof who a message came from.
* Routing authorization: a patient may only signal their *attending* nurse, and
  a nurse may only signal a patient they are attending (resolved from the
  Postgres ``patients`` table).  Anything else is rejected.
* This feature is completely independent of the ASR/NLP/TTS/alert pipeline and
  keeps working even if those modules are unavailable.
"""

import json
import logging
from typing import Dict, Optional

import asyncpg
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from auth import decode_token
from models import SignalMessage, SIGNAL_EVENTS
from pg_database import (
    get_conn,
    get_patient_by_user_id,
    get_patients_for_nurse,
    get_user_by_username,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Call Signaling"])


# ── Connection registry ───────────────────────────────────────────────────────

class SignalRegistry:
    """
    Tracks the live signaling WebSocket for every connected user, keyed by
    username, plus a lightweight "who is in a call with whom" map so the server
    can answer ``busy`` without ever inspecting media.

    Single-process, single-event-loop assumption (same as the nurse
    ConnectionManager). For multi-worker deployments this must move to a shared
    store (e.g. Redis pub/sub).
    """

    def __init__(self) -> None:
        self._sockets: Dict[str, WebSocket] = {}
        # username → the peer username they are currently in an active call with
        self._in_call_with: Dict[str, str] = {}
        # username → the peer they are currently negotiating with (set as soon
        # as an invite is routed / accepted). Used to route mid-call frames
        # (sdp/ice) even if the client omits `to`.
        self._peer: Dict[str, str] = {}

    # ── membership ────────────────────────────────────────────────────────────

    async def register(self, username: str, ws: WebSocket) -> None:
        # If the same user reconnects, drop the stale socket first.
        old = self._sockets.get(username)
        if old is not None and old is not ws:
            try:
                await old.close(code=4000)
            except Exception:
                pass
        self._sockets[username] = ws
        logger.info("Signaling connected: %s (total=%d)", username, len(self._sockets))

    def unregister(self, username: str, ws: WebSocket) -> None:
        # Only remove if the current socket is the one disconnecting (avoids a
        # reconnect race clobbering the fresh socket).
        if self._sockets.get(username) is ws:
            self._sockets.pop(username, None)
        self.clear_call(username)
        logger.info("Signaling disconnected: %s (total=%d)", username, len(self._sockets))

    def is_online(self, username: str) -> bool:
        return username in self._sockets

    def socket_for(self, username: str) -> Optional[WebSocket]:
        return self._sockets.get(username)

    # ── call bookkeeping ────────────────────────────────────────────────────--

    def is_busy(self, username: str) -> bool:
        return username in self._in_call_with

    def mark_in_call(self, a: str, b: str) -> None:
        self._in_call_with[a] = b
        self._in_call_with[b] = a

    def clear_call(self, username: str) -> None:
        peer = self._in_call_with.pop(username, None)
        if peer is not None:
            # Only clear the peer if it still points back at us.
            if self._in_call_with.get(peer) == username:
                self._in_call_with.pop(peer, None)
        # Clear negotiation peer both ways too.
        np = self._peer.pop(username, None)
        if np is not None and self._peer.get(np) == username:
            self._peer.pop(np, None)

    def set_peer(self, a: str, b: str) -> None:
        """Record the current negotiation partner both directions."""
        self._peer[a] = b
        self._peer[b] = a

    def peer_of(self, username: str) -> Optional[str]:
        return self._peer.get(username)

    # ── delivery ────────────────────────────────────────────────────────────--

    async def send_to(self, username: str, payload: dict) -> bool:
        """Deliver a JSON payload to a specific user. Returns False if offline."""
        ws = self._sockets.get(username)
        if ws is None:
            return False
        try:
            await ws.send_text(json.dumps(payload))
            return True
        except Exception as exc:
            logger.warning("Signaling send to %s failed: %s", username, exc)
            self._sockets.pop(username, None)
            return False


# Module-level singleton.
registry = SignalRegistry()


# ── Authorization helpers ─────────────────────────────────────────────────────

async def _authorized_pair(
    conn: asyncpg.Connection,
    sender_role: str,
    sender_username: str,
    sender_user_id: Optional[int],
    target_username: str,
) -> bool:
    """
    Return True if `sender` is allowed to signal `target`.

    A patient may only reach their attending nurse; a nurse may only reach a
    patient they attend. This prevents one patient/nurse from opening a call
    channel to an arbitrary user.
    """
    target = await get_user_by_username(conn, target_username)
    if target is None:
        return False
    target_role = target["role"]

    if sender_role == "patient" and target_role == "nurse":
        # The patient's own record names their attending nurse.
        patient = await get_patient_by_user_id(conn, sender_user_id) if sender_user_id else None
        return bool(patient and patient["attending"] == target_username)

    if sender_role == "nurse" and target_role == "patient":
        # The nurse must be the attending nurse of the target patient.
        patients = await get_patients_for_nurse(conn, sender_username)
        return any(p["username"] == target_username for p in patients)

    return False


async def _resolve_default_target(
    conn: asyncpg.Connection,
    sender_role: str,
    sender_user_id: Optional[int],
) -> Optional[str]:
    """
    When a patient starts a call without naming a target, route to their
    attending nurse automatically. Nurses must always name the patient.
    """
    if sender_role == "patient" and sender_user_id:
        patient = await get_patient_by_user_id(conn, sender_user_id)
        if patient and patient["attending"]:
            return patient["attending"]
    return None


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@router.websocket("/ws/signal")
async def signal_ws(
    websocket: WebSocket,
    token: str = Query(..., description="JWT access token"),
):
    """
    Persistent signaling socket for one user (patient or nurse).

    Client → server frames: any event in SIGNAL_EVENTS carrying `to` and,
    depending on the event, `sdp` / `candidate`.  Server → client frames are
    the relayed peer messages plus `connected` / `busy` / `peer_offline` /
    `error`.  A plain-text `"ping"`/`"pong"` keepalive is also supported.
    """
    await websocket.accept()
    logger.info("[SIGNAL] socket accepted, token_len=%d — authenticating", len(token or ""))

    # ── Authenticate ──────────────────────────────────────────────────────────
    try:
        payload = decode_token(token)
        username  = payload.get("sub")
        role      = payload.get("role", "")
        full_name = payload.get("full_name", username)
        user_id   = payload.get("user_id")
    except Exception as exc:
        logger.warning("[SIGNAL] AUTH REJECTED — invalid/expired token: %s", exc)
        await websocket.send_text(json.dumps({"event": "error", "reason": "Invalid or expired token"}))
        await websocket.close(code=4001)
        return

    if role not in ("patient", "nurse") or not username:
        logger.warning("[SIGNAL] ROLE REJECTED — user=%s role=%s", username, role)
        await websocket.send_text(json.dumps({"event": "error", "reason": "Only patient/nurse may signal"}))
        await websocket.close(code=4003)
        return

    logger.info("[SIGNAL] socket authenticated: user=%s role=%s — registering", username, role)
    await registry.register(username, websocket)
    logger.info("[SIGNAL] online users now: %s", list(registry._sockets.keys()))  # noqa: SLF001
    await websocket.send_text(json.dumps({
        "event": "connected",
        "from_user": username,
        "from_role": role,
    }))

    try:
        while True:
            raw = await websocket.receive_text()

            # Keepalive
            if raw == "ping":
                await websocket.send_text("pong")
                continue
            if raw == "pong":
                continue

            # ── Parse + validate frame ────────────────────────────────────────
            try:
                data = json.loads(raw)
                msg = SignalMessage(**data)
            except Exception:
                await websocket.send_text(json.dumps({"event": "error", "reason": "Malformed signaling frame"}))
                continue

            if msg.event not in SIGNAL_EVENTS:
                await websocket.send_text(json.dumps({"event": "error", "reason": f"Unknown event '{msg.event}'"}))
                continue

            # Resolve the routing target. Server-side identity always wins.
            target = msg.to

            if msg.event in ("call_invite", "call_accept", "call_reject", "call_cancel", "call_hangup"):
                logger.info("[SIGNAL] %s from=%s to=%s call_id=%s online=%s",
                            msg.event, username, msg.to, msg.call_id,
                            list(registry._sockets.keys()))  # noqa: SLF001

            # A patient invite may omit `to` → auto-route to attending nurse.
            if not target and msg.event == "call_invite":
                async for conn in get_conn():
                    target = await _resolve_default_target(conn, role, user_id)
                    logger.info("[SIGNAL] resolved default target for %s → %s", username, target)
                    break

            # For any mid-call frame (sdp/ice/hangup/etc.) that omits `to`, fall
            # back to the peer we recorded when the call was set up. This keeps a
            # patient who dialled with to=null working for the whole call.
            if not target:
                target = registry.peer_of(username)

            if not target:
                await websocket.send_text(json.dumps({
                    "event": "error", "call_id": msg.call_id,
                    "reason": "No target for this message",
                }))
                continue

            # Record the negotiation peer as soon as a call is being set up so
            # subsequent frames route even without an explicit `to`.
            if msg.event in ("call_invite", "call_accept"):
                registry.set_peer(username, target)

            # ── Authorize the pair (only enforced when starting a call) ───────
            # ICE/SDP/hangup frames within an already-authorized call are routed
            # directly; the initial call_invite/accept gate the relationship.
            if msg.event in ("call_invite", "call_accept"):
                async for conn in get_conn():
                    allowed = await _authorized_pair(conn, role, username, user_id, target)
                    break
                logger.info("[SIGNAL] authorize %s(%s) → %s : %s",
                            username, role, target, "ALLOWED" if allowed else "DENIED")
                if not allowed:
                    await websocket.send_text(json.dumps({
                        "event": "error", "call_id": msg.call_id,
                        "reason": "Not authorized to call this user",
                    }))
                    continue

            # ── Busy / offline checks on invite ───────────────────────────────
            if msg.event == "call_invite":
                if not registry.is_online(target):
                    await websocket.send_text(json.dumps({
                        "event": "peer_offline", "call_id": msg.call_id, "to": target,
                    }))
                    continue
                if registry.is_busy(target):
                    await websocket.send_text(json.dumps({
                        "event": "busy", "call_id": msg.call_id, "to": target,
                    }))
                    continue

            # ── Stamp authenticated sender identity, then relay ───────────────
            msg.from_user = username
            msg.from_name = full_name
            msg.from_role = role
            out = msg.relay_copy()

            delivered = await registry.send_to(target, out)
            if not delivered:
                await websocket.send_text(json.dumps({
                    "event": "peer_offline", "call_id": msg.call_id, "to": target,
                }))
                continue

            # ── Track call state for busy detection ───────────────────────────
            if msg.event == "call_accept":
                registry.mark_in_call(username, target)
            elif msg.event in ("call_hangup", "call_reject", "call_cancel"):
                registry.clear_call(username)

    except WebSocketDisconnect:
        logger.info("Signaling socket closed for %s", username)
    except Exception as exc:
        logger.warning("Signaling error for %s: %s", username, exc)
    finally:
        # Best-effort: tell the peer (if any) that this side dropped, then clean up.
        peer = registry._in_call_with.get(username)  # noqa: SLF001 (intentional internal read)
        if peer:
            await registry.send_to(peer, {
                "event": "call_hangup",
                "from_user": username,
                "reason": "peer_disconnected",
            })
        registry.unregister(username, websocket)
