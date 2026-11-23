"""
routers/audio.py — POST /audio/ingest

This is the single entry point for audio from the patient Android app.

Full request lifecycle
----------------------
1.  Validate content-type, file size, and room_id form field.
2.  Stream the WAV bytes to disk in WAV_TEMP_DIR (non-blocking via aiofiles).
3.  Run the pipeline (stub or real) to get transcript / intent / distress / priority.
4.  Persist an alert row in SQLite.
5.  Broadcast a WsAlertPayload to all connected nurse WebSocket clients.
6.  Return IngestResponse (HTTP 201) to the Android app.

The Android app does not wait for nurse acknowledgement — fire-and-forget from
its perspective.  The nurse side polls /alerts/latest or holds a WebSocket open.
"""

import logging
import uuid
from pathlib import Path

import aiofiles
import aiosqlite
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from config import settings
from database import get_db, insert_alert, utcnow
from models import AlertResponse, IngestResponse, WsAlertPayload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/audio", tags=["Audio Ingestion"])


# ── Lazy pipeline import ──────────────────────────────────────────────────────
# Importing at module level would fail if USE_STUB=False and the real pipeline
# deps (torch, etc.) are not installed yet.  Import lazily so the module always
# loads cleanly regardless of which pipeline is active.

def get_pipeline():
    """Return the active pipeline module (stub or real) based on USE_STUB."""
    if settings.USE_STUB:
        import pipeline_stub as pipeline
    else:
        import pipeline
    return pipeline


# ── WebSocket broadcaster — injected by main.py at startup ───────────────────
# Stored as a module-level callable so the router does not need to import the
# ws router (which would create a circular dependency).
_ws_broadcast = None

# request_key -> IngestResponse for recent uploads (bounded, single process).
from collections import OrderedDict
_INGEST_KEYS: "OrderedDict[str, IngestResponse]" = OrderedDict()

def set_broadcaster(fn):
    """Called from main.py after both routers are registered."""
    global _ws_broadcast
    _ws_broadcast = fn


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.post(
    "/ingest",
    response_model=IngestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Receive a WAV clip from the patient phone",
    description=(
        "Accepts a multipart/form-data POST with an `audio` file field (WAV) "
        "and a `room_id` text field.  Runs the AI pipeline, stores an alert, "
        "and pushes a WebSocket notification to connected nurse clients."
    ),
)
async def ingest_audio(
    audio:   UploadFile = File(..., description="WAV audio clip (16kHz mono PCM)"),
    room_id: str        = Form(..., description="Ward room identifier, e.g. '4B'"),
    lang:    str        = Form("", description="Optional language hint: en | hi | kn (blank = auto-detect)"),
    request_key: str    = Form("", description="Idempotency key from the app's retry queue"),
    db:      aiosqlite.Connection = Depends(get_db),
):
    # A retried upload (the app lost the first response) returns the original
    # result instead of paging the nurse twice.
    request_key = (request_key or "").strip()[:80]
    if request_key and request_key in _INGEST_KEYS:
        logger.info("Ingest retry with known request_key — returning original result")
        return _INGEST_KEYS[request_key]

    # ── 1. Validate room_id ───────────────────────────────────────────────────
    room_id = room_id.strip()
    if not room_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="room_id must not be empty",
        )

    # ── 2. Validate content-type ──────────────────────────────────────────────
    ct = (audio.content_type or "").lower()
    if ct and ct not in settings.ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported content-type '{ct}'. Expected one of {settings.ALLOWED_CONTENT_TYPES}",
        )

    # ── 3. Read and size-check the upload ─────────────────────────────────────
    # Read the full body here — WAV files from the app are typically < 1 MB for
    # a 2–5 s utterance.  MAX_WAV_SIZE_MB is an additional server-side guard.
    max_bytes = settings.MAX_WAV_SIZE_MB * 1024 * 1024
    wav_bytes = await audio.read(max_bytes + 1)

    if len(wav_bytes) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Upload exceeds maximum size of {settings.MAX_WAV_SIZE_MB} MB",
        )

    if len(wav_bytes) < 44:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File too small to be a valid WAV (must be at least 44 bytes)",
        )

    logger.info("Received audio from room=%s size=%d bytes", room_id, len(wav_bytes))

    # ── 4. Save WAV to disk asynchronously ────────────────────────────────────
    # File is stored for audit purposes and future wav2vec re-processing.
    # Filename: {room_id}_{uuid4}.wav  — unique, no PII in the filename itself.
    wav_filename = f"{room_id}_{uuid.uuid4().hex}.wav"
    wav_path     = settings.WAV_TEMP_DIR / wav_filename

    async with aiofiles.open(wav_path, "wb") as f:
        await f.write(wav_bytes)

    logger.debug("WAV saved: %s", wav_path)

    # ── 5. Run the pipeline ───────────────────────────────────────────────────
    # Whisper on GPU takes ~1-3s for a short clip; allow up to 60s for safety.
    try:
        import asyncio
        pipeline = get_pipeline()
        lang_hint = (lang or "").strip().lower() or None
        # Stub pipeline doesn't take a lang hint; only pass it to the real one.
        if settings.USE_STUB:
            result = await asyncio.wait_for(
                pipeline.process_audio(wav_bytes, room_id), timeout=60.0
            )
        else:
            result = await asyncio.wait_for(
                pipeline.process_audio(wav_bytes, room_id, lang_hint=lang_hint),
                timeout=60.0,
            )
    except Exception as exc:
        logger.exception("Pipeline error for room=%s", room_id)
        # Do NOT expose internal stack trace to the client.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AI pipeline error — audio was saved and will be retried",
        ) from exc

    # ── 6-8. Triage -> persist -> dispatch (shared with the patient WebSocket) ─
    # Triage runs BEFORE the alert is stored: nurse-bound intents go straight to
    # the nurse (router skipped); ground-support requests go to housekeeping and
    # never page the nurse; food becomes a Routine nurse-visible request.
    alert_language = getattr(result, "language", None) or lang_hint
    from ingest_flow import handle_pipeline_result
    out = await handle_pipeline_result(
        db, result=result, room_id=room_id, wav_path=str(wav_path),
        language=alert_language, broadcast=_ws_broadcast,
    )

    # should_alert tells the app whether someone was notified: true when the
    # nurse was paged OR a department took the request.
    notified = out["nurse_paged"] or bool(out["departments"])
    if out["request_class"] == "facility" and out["departments"]:
        msg = "Sent to housekeeping"
    elif out["request_class"] == "food":
        msg = "Food request noted — your attender or nurse will place the order"
    elif notified:
        msg = "Nurse notified"
    elif (result.transcript or "").strip() in ("", "[silence]"):
        msg = "No speech heard"
    else:
        msg = "No request detected"
    no_speech = (result.transcript or "").strip() in ("", "[silence]")

    resp = IngestResponse(
        alert_id       = out["alert_id"],
        room_id        = room_id,
        priority       = out["priority"],
        intent         = out["intent"],
        distress_score = result.distress_score,
        transcript     = "" if no_speech else result.transcript,
        is_stub        = result.is_stub,
        summary        = result.summary,
        emotion        = result.emotion,
        should_alert   = notified,
        message        = msg,
    )
    if request_key:
        _INGEST_KEYS[request_key] = resp
        while len(_INGEST_KEYS) > 300:
            _INGEST_KEYS.popitem(last=False)
    return resp
