"""WebRTC signaling WebSocket for nurse <-> patient voice calls.

The WebSocket carries signaling only (offer/answer/ICE and call state). Actual
microphone audio travels peer-to-peer through WebRTC and is not sent through
FastAPI.
"""

import asyncio
import logging

from dataclasses import dataclass
from typing import Dict, Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from auth import decode_token
from config import settings
from database import get_alert_by_id, get_db

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Call Signaling"])


@dataclass
class CallSession:
    call_id: str
    alert_id: int
    room_id: str
    nurse_id: str
    nurse_name: str
    nurse_ws: Optional[WebSocket] = None
    patient_ws: Optional[WebSocket] = None


class CallManager:
    def __init__(self) -> None:
        self.nurses: Dict[str, WebSocket] = {}
        self.patients: Dict[str, WebSocket] = {}
        self.calls: Dict[str, CallSession] = {}
        self._lock = asyncio.Lock()

    async def register_nurse(self, nurse_id: str, ws: WebSocket) -> None:
        async with self._lock:
            old = self.nurses.get(nurse_id)
            if old is not None and old is not ws:
                try:
                    await old.close(code=4008)
                except Exception:
                    pass
            self.nurses[nurse_id] = ws

    async def unregister_nurse(self, nurse_id: str, ws: WebSocket) -> None:
        async with self._lock:
            if self.nurses.get(nurse_id) is ws:
                self.nurses.pop(nurse_id, None)
            for call_id, call in list(self.calls.items()):
                if call.nurse_ws is ws:
                    call.nurse_ws = None
                    if call.patient_ws is not None:
                        await self._safe_send(call.patient_ws, {
                            "event": "call_ended",
                            "call_id": call_id,
                            "reason": "nurse_disconnected",
                        })
                        self.calls.pop(call_id, None)

    async def register_patient(self, room_id: str, ws: WebSocket) -> None:
        async with self._lock:
            old = self.patients.get(room_id)
            if old is not None and old is not ws:
                try:
                    await old.close(code=4008)
                except Exception:
                    pass
            self.patients[room_id] = ws

    async def unregister_patient(self, room_id: str, ws: WebSocket) -> None:
        async with self._lock:
            if self.patients.get(room_id) is ws:
                self.patients.pop(room_id, None)
            for call_id, call in list(self.calls.items()):
                if call.patient_ws is ws:
                    call.patient_ws = None
                    if call.nurse_ws is not None:
                        await self._safe_send(call.nurse_ws, {
                            "event": "call_ended",
                            "call_id": call_id,
                            "reason": "patient_disconnected",
                        })
                        self.calls.pop(call_id, None)

    async def start_call(self, call_id: str, alert_id: int, room_id: str,
                         nurse_id: str, nurse_name: str) -> tuple[bool, str]:
        async with self._lock:
            patient_ws = self.patients.get(room_id)
            nurse_ws = self.nurses.get(nurse_id)
            if patient_ws is None:
                return False, "patient_offline"
            if nurse_ws is None:
                return False, "nurse_offline"
            if any(c.room_id == room_id for c in self.calls.values()):
                return False, "patient_busy"
            call = CallSession(call_id, alert_id, room_id, nurse_id, nurse_name,
                               nurse_ws=nurse_ws, patient_ws=patient_ws)
            self.calls[call_id] = call
            await self._safe_send(patient_ws, {
                "event": "incoming_call",
                "call_id": call_id,
                "alert_id": alert_id,
                "room_id": room_id,
                "nurse_id": nurse_id,
                "nurse_name": nurse_name,
            })
            return True, "ringing"

    async def relay(self, call_id: str, sender: WebSocket, payload: dict) -> bool:
        async with self._lock:
            call = self.calls.get(call_id)
            if call is None:
                return False
            target = call.patient_ws if sender is call.nurse_ws else call.nurse_ws
            if target is None:
                return False
            await self._safe_send(target, payload)
            return True

    async def accept(self, call_id: str, patient_ws: WebSocket) -> bool:
        return await self._state_to_other(call_id, patient_ws, "call_accepted")

    async def reject(self, call_id: str, patient_ws: WebSocket) -> bool:
        ok = await self._state_to_other(call_id, patient_ws, "call_rejected")
        await self.end_call(call_id, "rejected")
        return ok

    async def end_call(self, call_id: str, reason: str = "ended") -> bool:
        async with self._lock:
            call = self.calls.pop(call_id, None)
            if call is None:
                return False
            payload = {"event": "call_ended", "call_id": call_id, "reason": reason}
            if call.nurse_ws is not None:
                await self._safe_send(call.nurse_ws, payload)
            if call.patient_ws is not None:
                await self._safe_send(call.patient_ws, payload)
            return True

    async def _state_to_other(self, call_id: str, sender: WebSocket, event: str) -> bool:
        async with self._lock:
            call = self.calls.get(call_id)
            if call is None:
                return False
            target = call.nurse_ws if sender is call.patient_ws else call.patient_ws
            if target is None:
                return False
            await self._safe_send(target, {"event": event, "call_id": call_id})
            return True

    @staticmethod
    async def _safe_send(ws: WebSocket, payload: dict) -> None:
        try:
            await ws.send_json(payload)
        except Exception as exc:
            logger.debug("Call signaling send failed: %s", exc)


manager = CallManager()


def _auth(token: str, expected_role: str) -> dict:
    payload = decode_token(token)
    if payload.get("role") != expected_role:
        raise ValueError(f"{expected_role} role required")
    return payload


@router.websocket("/ws/call")
async def call_ws(
    websocket: WebSocket,
    token: str = Query(...),
    role: str = Query(...),
    nurse_id: str = Query(default=""),
    room_id: str = Query(default=""),
):
    await websocket.accept()

    try:
        payload = _auth(token, role)
    except Exception:
        await websocket.send_json({"event": "error", "message": "Invalid call credentials"})
        await websocket.close(code=4001)
        return

    if role == "nurse":
        nurse_id = nurse_id.strip() or str(payload.get("user_id") or payload.get("sub") or "nurse")
        nurse_name = payload.get("full_name") or payload.get("sub") or "Nurse"
        await manager.register_nurse(nurse_id, websocket)
        await websocket.send_json({"event": "call_connected", "role": "nurse"})
    elif role == "patient":
        room_id = room_id.strip()
        if not room_id or room_id == "—":
            await websocket.send_json({"event": "error", "message": "room_id is required"})
            await websocket.close(code=4002)
            return
        await manager.register_patient(room_id, websocket)
        await websocket.send_json({"event": "call_connected", "role": "patient", "room_id": room_id})
    else:
        await websocket.close(code=4003)
        return

    try:
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive_text(), timeout=60)
            except asyncio.TimeoutError:
                await websocket.send_text("ping")
                continue
            if message == "pong":
                continue
            try:
                data = __import__("json").loads(message)
            except Exception:
                continue

            event = data.get("event")
            call_id = str(data.get("call_id") or "")
            if not call_id and event not in {"call_request"}:
                continue

            if role == "nurse" and event == "call_request":
                try:
                    alert_id = int(data.get("alert_id"))
                    requested_room = str(data.get("room_id") or "").strip()
                    alert = None
                    async for db in get_db():
                        alert = await get_alert_by_id(db, alert_id)
                        break
                    if alert is None:
                        raise ValueError("alert_not_found")
                    if alert["room_id"].strip() != requested_room:
                        raise ValueError("room_mismatch")
                    if alert["priority"] != "Routine":
                        raise ValueError("calls_are_for_routine_only")
                    if alert["attended"]:
                        raise ValueError("alert_already_attended")
                except Exception as exc:
                    await websocket.send_json({
                        "event": "call_failed",
                        "call_id": call_id,
                        "reason": str(exc),
                    })
                    continue

                ok, reason = await manager.start_call(
                    call_id=call_id,
                    alert_id=alert_id,
                    room_id=requested_room,
                    nurse_id=nurse_id,
                    nurse_name=nurse_name,
                )
                await websocket.send_json({
                    "event": "call_started" if ok else "call_failed",
                    "call_id": call_id,
                    "reason": reason,
                })
                continue

            if role == "patient" and event == "call_accept":
                await manager.accept(call_id, websocket)
                continue
            if role == "patient" and event == "call_reject":
                await manager.reject(call_id, websocket)
                continue
            if event == "call_end":
                await manager.end_call(call_id, str(data.get("reason") or "ended"))
                continue

            if event in {"offer", "answer", "ice_candidate"}:
                data["event"] = event
                await manager.relay(call_id, websocket, data)

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("Call WS error (%s): %s", role, exc)
    finally:
        if role == "nurse":
            await manager.unregister_nurse(nurse_id, websocket)
        else:
            await manager.unregister_patient(room_id, websocket)
