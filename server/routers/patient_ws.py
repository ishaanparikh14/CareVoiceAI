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

import aiosqlite
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from auth import decode_token
from database import get_db, insert_alert, utcnow
from models import AlertResponse, WsAlertPayload
from vad_session import VADSession, float32_to_wav
from config import settings
from routers.audio import get_pipeline

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Patient WebSocket"])

# Injected from main.py at startup (same pattern as nurse WS broadcaster)
_nurse_broadcast = None

def set_nurse_broadcaster(fn):
    global _nurse_broadcast
    _nurse_broadcast = fn


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
            import uuid, aiofiles
            wav_filename = f"{room_id}_{uuid.uuid4().hex}.wav"
            wav_path     = settings.WAV_TEMP_DIR / wav_filename
            async with aiofiles.open(wav_path, "wb") as f:
                await f.write(wav_bytes)

            # Insert alert into SQLite
            async with aiosqlite.connect(settings.DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                alert_id = await insert_alert(
                    db,
                    room_id        = room_id,
                    priority       = result.priority.value,
                    intent         = result.intent.value,
                    distress_score = result.distress_score,
                    transcript     = result.transcript,
                    wav_path       = str(wav_path),
                )
                await db.commit()

            logger.info(
                "Alert created via WS: id=%d room=%s priority=%s intent=%s",
                alert_id, room_id, result.priority.value, result.intent.value,
            )

            # Broadcast to nurses
            if _nurse_broadcast is not None:
                alert_resp = AlertResponse(
                    id             = alert_id,
                    room_id        = room_id,
                    priority       = result.priority,
                    intent         = result.intent,
                    distress_score = result.distress_score,
                    transcript     = result.transcript,
                    wav_path       = str(wav_path),
                    acknowledged   = False,
                    created_at     = utcnow(),
                )
                payload_json = WsAlertPayload.from_alert_response(alert_resp).model_dump_json()
                await _nurse_broadcast(payload_json)

            # Confirm to patient
            await websocket.send_json({
                "event":        "alert_sent",
                "alert_id":     alert_id,
                "priority":     result.priority.value,
                "intent":       result.intent.value,
                "distress":     result.distress_score,
                "transcript":   result.transcript,
                "is_stub":      result.is_stub,
                "message":      "Your nurse has been notified ✓",
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
        logger.info("VAD session released for patient %s", username)
