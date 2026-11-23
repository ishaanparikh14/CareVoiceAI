"""
routers/patient_ws.py — always-on patient WebSocket endpoint.

Endpoint
--------
WS /ws/patient?token=<jwt>&room_id=<room>

Flow
----
1.  Patient browser opens the page → mic permission requested immediately.
2.  JS streams 512-sample (32 ms) binary frames at 16 kHz over this WebSocket.
3.  Server feeds each frame into a per-connection VADSession.
4.  When VAD detects a complete utterance the server:
      a. Encodes float32 → WAV bytes
      b. Runs the AI pipeline (stub or real)
      c. Inserts an alert into SQLite
      d. Broadcasts a WsAlertPayload to all nurse connections
      e. Sends a JSON confirmation back to the patient client
5.  The patient client shows a "Nurse notified ✓" toast without any button press.

Authentication
--------------
JWT is passed as a query param (?token=...) because the browser WebSocket API
does not support custom headers.  The token is verified before the connection
is accepted.  401 close code on failure.

Binary frame format
-------------------
Each frame: exactly 512 × 4 = 2048 bytes, little-endian float32, values [-1, 1].
"""

import asyncio
import logging
import uuid

import aiofiles
import aiosqlite
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from auth import decode_token
from database import get_db, insert_alert, utcnow
from models import AlertResponse, WsAlertPayload
from vad_session import VADSession, float32_to_wav
from config import settings
from routers.audio import get_pipeline
from pg_database import get_patient_name_by_room

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Patient WebSocket"])

# Injected from main.py at startup (same pattern as nurse WS broadcaster)
_nurse_broadcast = None

def set_nurse_broadcaster(fn):
    global _nurse_broadcast
    _nurse_broadcast = fn


# ── Room-indexed patient socket registry ──────────────────────────────────────
# Maps room_id -> set of that room's live patient WebSockets, so other modules
# (department_dispatch) can push feedback like "sent to canteen" / "done" to the
# patient without a button press. Single-process assumption, like the nurse WS.
import asyncio as _asyncio
_patient_socks: dict[str, set] = {}


def _register_patient(room_id: str, ws) -> None:
    _patient_socks.setdefault(room_id, set()).add(ws)


def _unregister_patient(room_id: str, ws) -> None:
    socks = _patient_socks.get(room_id)
    if socks:
        socks.discard(ws)
        if not socks:
            _patient_socks.pop(room_id, None)


async def notify_room(room_id: str, payload: dict) -> bool:
    """Push a JSON payload to every live patient socket in a room. Best-effort;
    returns True if at least one delivery succeeded."""
    socks = list(_patient_socks.get(room_id, set()))
    if not socks:
        return False
    results = await _asyncio.gather(
        *[ws.send_json(payload) for ws in socks], return_exceptions=True
    )
    return any(not isinstance(r, Exception) for r in results)


@router.websocket("/ws/patient")
async def patient_ws(
    websocket: WebSocket,
    token:   str = Query(..., description="JWT access token"),
    room_id: str = Query(..., description="Patient room number, e.g. 4B"),
):
    # ── 1. Authenticate — must accept first, THEN close if invalid
    #       (FastAPI/Starlette cannot close before accept)
    await websocket.accept()

    try:
        payload = decode_token(token)
        username  = payload.get("sub", "unknown")
        user_id   = payload.get("user_id")
        full_name = payload.get("full_name", username)
        role      = payload.get("role", "")
    except Exception:
        await websocket.send_json({"event": "error", "message": "Invalid or expired token"})
        await websocket.close(code=4001)
        return

    if role != "patient":
        await websocket.send_json({"event": "error", "message": "Patient role required"})
        await websocket.close(code=4003)
        return

    # Sanitise room_id
    room_id = room_id.strip()
    if not room_id or room_id == '—':
        await websocket.send_json({"event": "error", "message": "room_id is required"})
        await websocket.close(code=4002)
        return

    logger.info("Patient WS connected: %s (room=%s)", username, room_id)
    _register_patient(room_id, websocket)

    # Notify client the connection is live
    await websocket.send_json({
        "event":    "connected",
        "username": username,
        "room_id":  room_id,
        "message":  "Listening… speak to call your nurse.",
    })

    # ── 2. Create VAD session for this connection ─────────────────────────────
    vad = VADSession(patient_id=username)

    # ── 3. Main receive loop ──────────────────────────────────────────────────
    try:
        while True:
            # Receive either binary audio frame or text control message
            msg = await asyncio.wait_for(
                websocket.receive(),
                timeout=settings.WS_KEEPALIVE_SECONDS,
            )

            if msg["type"] == "websocket.disconnect":
                break

            # ── Text control frames ───────────────────────────────────────────
            if msg.get("text"):
                text = msg["text"]
                if text == "ping":
                    await websocket.send_text("pong")
                elif text == "stop":
                    # Patient tapped mute — reset VAD, send ack
                    vad.reset()
                    await websocket.send_json({"event": "stopped"})
                    logger.info("VAD reset by patient %s (stop command)", username)
                elif text == "start":
                    vad.reset()
                    await websocket.send_json({
                        "event":   "listening",
                        "message": "Listening… speak to call your nurse.",
                    })
                    logger.info("VAD restarted by patient %s", username)
                continue

            # ── Binary audio frame ────────────────────────────────────────────
            raw = msg.get("bytes")
            if not raw:
                continue

            utterance = vad.process_chunk(raw)

            if utterance is None:
                # Still accumulating — optionally send VAD state to client
                # (kept silent to avoid flooding the connection)
                continue

            # ── Utterance detected — run pipeline ─────────────────────────────
            logger.info(
                "Utterance detected: patient=%s room=%s samples=%d (%.2fs)",
                username, room_id, len(utterance), len(utterance) / 16000,
            )

            # Notify patient immediately so UI can update without waiting for pipeline
            await websocket.send_json({
                "event":   "speech_detected",
                "message": "Speech detected — notifying your nurse…",
            })

            # Encode to WAV
            wav_bytes = float32_to_wav(utterance)

            # Run pipeline (stub or real)
            try:
                pipeline = get_pipeline()
                result   = await pipeline.process_audio(wav_bytes, room_id)
            except Exception as exc:
                logger.exception("Pipeline error for patient %s", username)
                await websocket.send_json({
                    "event":   "error",
                    "message": "Processing error — please try again.",
                })
                continue

            # Save WAV to disk
            wav_filename = f"{room_id}_{uuid.uuid4().hex}.wav"
            wav_path     = settings.WAV_TEMP_DIR / wav_filename
            async with aiofiles.open(wav_path, "wb") as f:
                await f.write(wav_bytes)

            # Triage -> persist -> dispatch (shared with POST /audio/ingest).
            async with aiosqlite.connect(settings.DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("PRAGMA busy_timeout=5000")
                from ingest_flow import handle_pipeline_result
                out = await handle_pipeline_result(
                    db, result=result, room_id=room_id, wav_path=str(wav_path),
                    language=getattr(result, "language", None), broadcast=_nurse_broadcast,
                )
            alert_id = out["alert_id"]
            if out["request_class"] == "facility" and out["departments"]:
                confirm = "Sent to housekeeping ✓"
            elif out["request_class"] == "food":
                confirm = "Food request noted — your attender or nurse will order it ✓"
            elif out["nurse_paged"]:
                confirm = "Your nurse has been notified ✓"
            else:
                confirm = "I didn't catch a request. Say \"Help\" and try again."

            # Confirm to patient
            await websocket.send_json({
                "event":        "alert_sent" if (out["nurse_paged"] or out["departments"] or out["request_class"] == "food") else "no_request",
                "alert_id":     alert_id,
                "priority":     out["priority"].value,
                "intent":       out["intent"].value,
                "request_class": out["request_class"],
                "distress":     result.distress_score,
                "transcript":   result.transcript,
                "is_stub":      result.is_stub,
                "message":      confirm,
            })

    except asyncio.TimeoutError:
        # Keepalive ping
        try:
            await websocket.send_text("ping")
        except Exception:
            pass
    except WebSocketDisconnect:
        logger.info("Patient %s disconnected (WebSocketDisconnect)", username)
    except Exception as exc:
        logger.warning("Patient %s WS error: %s", username, exc)
    finally:
        vad.reset()
        _unregister_patient(room_id, websocket)
        logger.info("VAD session released for patient %s", username)
