"""
routers/voice_notes.py — bidirectional nurse/patient voice notes.

A voice note is a short WAV clip attached to a room (and optionally an alert).
Either side can record and send one; the other side lists and plays them.

The clip is transcribed once with the existing Whisper pipeline so the stored
`original_text` can be shown and spoken aloud on the receiving device. There is
NO server-side translation — the recipient's device chooses the TTS voice from
the note's own `language` field.

Storage:
  • Audio files live on disk under STORAGE_DIR/voice_notes.
  • Metadata + transcript live in the PostgreSQL `voice_notes` table.
"""

import asyncio
import logging
import uuid

import aiofiles
import asyncpg
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from auth import CurrentUser
from config import settings
from pg_database import (
    get_conn, create_voice_note, get_voice_notes, get_voice_note,
    get_patients_for_nurse,
)
from pipeline import _transcribe

router = APIRouter(prefix="/voice-notes", tags=["Voice Notes"])

log = logging.getLogger(__name__)

VOICE_DIR = settings.STORAGE_DIR / "voice_notes"
VOICE_DIR.mkdir(parents=True, exist_ok=True)

_MAX_BYTES = 10 * 1024 * 1024          # 10 MB hard cap
_ALLOWED_LANGS = {"en", "hi"}


class VoiceNoteResponse(BaseModel):
    id:            int
    room_id:       str
    alert_id:      int | None = None
    sender_role:   str
    sender_name:   str
    created_at:    str
    duration_ms:   int
    language:      str = "en"
    audio_url:     str
    original_text: str | None = None


def _to_response(row) -> VoiceNoteResponse:
    created = row["created_at"]
    created_s = created.isoformat() if hasattr(created, "isoformat") else str(created)
    return VoiceNoteResponse(
        id            = row["id"],
        room_id       = row["room_id"],
        alert_id      = row["alert_id"],
        sender_role   = row["sender_role"],
        sender_name   = row["sender_name"],
        created_at    = created_s,
        duration_ms   = row["duration_ms"],
        language      = row["language"] or "en",
        audio_url     = f"/voice-notes/{row['id']}/audio",
        original_text = row["original_text"],
    )


@router.post("", response_model=VoiceNoteResponse, status_code=status.HTTP_201_CREATED)
async def upload_voice_note(
    current_user: CurrentUser,
    audio:        UploadFile = File(...),
    room_id:      str = Form(...),
    alert_id:     int | None = Form(default=None),
    duration_ms:  int = Form(default=0, ge=0, le=120000),
    language:     str = Form(default="en"),
    conn:         asyncpg.Connection = Depends(get_conn),
):
    """Record a voice note: validate, save WAV, transcribe, store metadata."""
    room_id = room_id.strip()
    if not room_id:
        raise HTTPException(422, "room_id is required")
    if language not in _ALLOWED_LANGS:
        raise HTTPException(422, "language must be 'en' or 'hi'")

    data = await audio.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise HTTPException(413, "Voice note exceeds 10 MB")
    if len(data) < 44 or data[:4] != b"RIFF":
        raise HTTPException(400, "Voice note must be a valid WAV file")

    filename = f"{room_id}_{uuid.uuid4().hex}.wav"
    path = VOICE_DIR / filename
    async with aiofiles.open(path, "wb") as f:
        await f.write(data)

    try:
        # Transcribe with the existing Whisper pipeline (sender's language hint).
        try:
            transcript, _detected = await asyncio.to_thread(_transcribe, data, language)
        except Exception as exc:
            log.warning("Voice-note STT failed (storing without transcript): %s", exc)
            transcript = ""

        note_id = await create_voice_note(
            conn,
            room_id        = room_id,
            alert_id       = alert_id,
            sender_user_id = int(current_user["user_id"]),
            sender_role    = current_user["role"],
            sender_name    = current_user["full_name"],
            filename       = filename,
            mime_type      = "audio/wav",
            duration_ms    = duration_ms,
            language       = language,
            original_text  = transcript,
        )
    except Exception:
        path.unlink(missing_ok=True)
        raise

    row = await get_voice_note(conn, note_id)
    log.info("Voice note %d stored (room=%s sender=%s lang=%s)",
             note_id, room_id, current_user["role"], language)
    return _to_response(row)


@router.get("/latest", response_model=list[VoiceNoteResponse])
async def latest_voice_notes(
    current_user: CurrentUser,
    room_id:      str,
    conn:         asyncpg.Connection = Depends(get_conn),
):
    """List the most recent voice notes for a room (newest first)."""
    room_id = room_id.strip()
    if not room_id:
        raise HTTPException(422, "room_id is required")
    rows = await get_voice_notes(conn, room_id)
    return [_to_response(r) for r in rows]


@router.get("/inbox", response_model=list[VoiceNoteResponse])
async def nurse_inbox(
    current_user: CurrentUser,
    conn:         asyncpg.Connection = Depends(get_conn),
):
    """
    Nurse-only inbox: the most recent voice notes across every room assigned to
    the calling nurse, newest first. This is how a nurse discovers notes a
    patient sent without having to know the room up front.
    """
    if current_user["role"] != "nurse":
        raise HTTPException(403, "Nurse role required")

    patients = await get_patients_for_nurse(conn, current_user["username"])
    rooms = [p["room_number"] for p in patients if p["room_number"]]
    if not rooms:
        return []

    collected: list = []
    for room in rooms:
        collected.extend(await get_voice_notes(conn, room))
    # Sort the merged set newest-first and cap the list.
    collected.sort(key=lambda r: r["created_at"], reverse=True)
    return [_to_response(r) for r in collected[:50]]


@router.get("/{note_id}/audio")
async def get_voice_note_audio(
    note_id:      int,
    current_user: CurrentUser,
    conn:         asyncpg.Connection = Depends(get_conn),
):
    """Stream the stored WAV for a voice note."""
    row = await get_voice_note(conn, note_id)
    if row is None:
        raise HTTPException(404, "Voice note not found")
    path = VOICE_DIR / row["filename"]
    if not path.exists():
        raise HTTPException(404, "Voice note file is missing")
    return FileResponse(path, media_type=row["mime_type"], filename=row["filename"])
