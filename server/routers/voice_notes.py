"""PostgreSQL-backed bidirectional nurse/patient voice notes."""
import logging
import uuid
import asyncio
from pathlib import Path
import aiofiles
import asyncpg
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from auth import CurrentUser
from config import settings
from database import get_db, create_voice_note, get_voice_notes, get_voice_note
from pipeline import _transcribe
from llm_service import translate_text
from pg_database import get_nurse_preferred_language_for_room

router = APIRouter(prefix="/voice-notes", tags=["Voice Notes"])
VOICE_DIR = settings.STORAGE_DIR / "voice_notes"
VOICE_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger(__name__)


class VoiceNoteResponse(BaseModel):
    id: int
    room_id: str
    alert_id: int | None = None
    sender_role: str
    sender_name: str
    created_at: str
    duration_ms: int
    language: str = "en"
    audio_url: str
    original_text: str | None = None
    translated_text: str | None = None
    source_language: str | None = None
    target_language: str | None = None


@router.post("", response_model=VoiceNoteResponse, status_code=status.HTTP_201_CREATED)
async def upload_voice_note(
    current_user: CurrentUser,
    audio: UploadFile = File(...),
    room_id: str = Form(...),
    alert_id: int | None = Form(default=None),
    duration_ms: int = Form(default=0, ge=0, le=120000),
    language: str = Form(default="en"),
    db: asyncpg.Connection = Depends(get_db),
):
    room_id = room_id.strip()
    if not room_id:
        raise HTTPException(422, "room_id is required")
    if language not in {"en", "hi"}:
        raise HTTPException(422, "language must be 'en' or 'hi'")

    data = await audio.read(10 * 1024 * 1024 + 1)
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, "Voice note exceeds 10 MB")
    if len(data) < 44 or data[:4] != b"RIFF":
        raise HTTPException(400, "Voice note must be a valid WAV file")

    filename = f"{room_id}_{uuid.uuid4().hex}.wav"
    path = VOICE_DIR / filename
    async with aiofiles.open(path, "wb") as f:
        await f.write(data)

    try:
        # ── STT ──────────────────────────────────────────────────────────────
        try:
            transcript, detected_lang = await asyncio.to_thread(_transcribe, data)
        except Exception as e:
            log.warning("STT failed for voice note: %s", e)
            transcript = ""
            detected_lang = language

        original_text   = transcript
        source_language = language   # language spoken by the sender

        # ── Determine target language ────────────────────────────────────────
        #
        # IMPORTANT: `language` (the form field) is the SENDER's language.
        # It must NEVER be used as the translation target.
        #
        # Patient → Nurse: target = nurse's preferred_language (from users table)
        # Nurse → Patient: target = patient's language (from alert or last note)
        #
        sender_role     = current_user["role"]
        target_language = "en"   # safe default

        if sender_role == "nurse":
            # ── Nurse → Patient ──────────────────────────────────────────────
            alert_row = None
            if alert_id:
                alert_row = await db.fetchrow(
                    "SELECT language FROM alerts WHERE id=$1", alert_id
                )
            if alert_row:
                target_language = alert_row["language"]
            else:
                latest_patient = await db.fetchrow(
                    """SELECT language FROM voice_notes
                       WHERE room_id=$1 AND sender_role='patient'
                       ORDER BY created_at DESC LIMIT 1""",
                    room_id,
                )
                if latest_patient:
                    target_language = latest_patient["language"]

            log.info(
                "VOICE_TRANSLATION_DEBUG:\ndirection=NURSE_TO_PATIENT\n"
                "room_id=%s\nsourceLanguage=%s\ntargetLanguage=%s\noriginalText=%s",
                room_id, source_language, target_language, original_text,
            )

        else:
            # ── Patient → Nurse ──────────────────────────────────────────────
            # Look up the attending nurse's preferred_language from the users
            # table via a two-stage query (attending JOIN → voice-note history).
            # The lookup is in pg_database.get_nurse_preferred_language_for_room
            # and logs every failure path explicitly.
            nurse_pref = await get_nurse_preferred_language_for_room(db, room_id)
            target_language = nurse_pref

            log.info(
                "VOICE_TRANSLATION_DEBUG:\ndirection=PATIENT_TO_NURSE\n"
                "room_id=%s\nsourceLanguage=%s\nnursePrefLanguage=%s\n"
                "targetLanguage=%s\noriginalText=%s",
                room_id, source_language, nurse_pref, target_language, original_text,
            )

            # Warn explicitly when the lookup could not distinguish from the
            # source language — this is the condition that causes translation
            # to be skipped and is the primary known failure mode.
            if target_language == source_language:
                log.warning(
                    "VOICE_TRANSLATION_DEBUG:\nNURSE_LANG_SYNC_WARNING\n"
                    "room_id=%s\n"
                    "targetLanguage=%s equals sourceLanguage=%s → "
                    "translation WILL BE SKIPPED.\n"
                    "ACTION REQUIRED: ensure patients.attending is set to the "
                    "nurse's login username AND the nurse has called "
                    "PATCH /auth/me {preferred_language} with their chosen language.",
                    room_id, target_language, source_language,
                )

        # Validate
        if target_language not in {"en", "hi"}:
            target_language = "en"

        # ── Translation ──────────────────────────────────────────────────────
        log.info(
            "VOICE_TRANSLATION_DEBUG:\nTRANSLATION_REQUEST\n"
            "source=%s\ntarget=%s\ntext=%s",
            source_language, target_language, original_text,
        )

        if source_language == target_language or not original_text.strip():
            translated_text = original_text
            log.info(
                "VOICE_TRANSLATION_DEBUG:\nTRANSLATION_SKIPPED (same lang or empty)\n"
                "source=%s target=%s", source_language, target_language,
            )
        else:
            translated_text = await translate_text(original_text, target_language)
            log.info(
                "VOICE_TRANSLATION_DEBUG:\nTRANSLATION_RESPONSE\n"
                "source=%s\ntarget=%s\ntranslatedText=%s",
                source_language, target_language, translated_text,
            )

        note_id = await create_voice_note(
            db,
            room_id=room_id,
            alert_id=alert_id,
            sender_user_id=int(current_user["user_id"]),
            sender_role=current_user["role"],
            sender_name=current_user["full_name"],
            filename=filename,
            mime_type="audio/wav",
            duration_ms=duration_ms,
            language=language,
            original_text=original_text,
            translated_text=translated_text,
            source_language=source_language,
            target_language=target_language,
        )

        log.info(
            "VOICE_TRANSLATION_DEBUG:\nNOTE_SAVED\nvoiceNoteId=%s\n"
            "senderRole=%s\nsourceLanguage=%s\ntargetLanguage=%s\n"
            "originalText=%s\ntranslatedText=%s",
            note_id, sender_role, source_language, target_language,
            original_text, translated_text,
        )
    except Exception:
        path.unlink(missing_ok=True)
        raise

    row = await get_voice_note(db, note_id)
    created = row["created_at"].isoformat() if hasattr(row["created_at"], "isoformat") else str(row["created_at"])
    return VoiceNoteResponse(
        id=note_id, room_id=room_id, alert_id=alert_id,
        sender_role=current_user["role"], sender_name=current_user["full_name"],
        created_at=created, duration_ms=duration_ms, language=language,
        audio_url=f"/voice-notes/{note_id}/audio",
        original_text=original_text,
        translated_text=translated_text,
        source_language=source_language,
        target_language=target_language,
    )


@router.get("/latest", response_model=list[VoiceNoteResponse])
async def latest_voice_notes(current_user: CurrentUser, room_id: str, db: asyncpg.Connection = Depends(get_db)):
    room_id = room_id.strip()
    if not room_id:
        raise HTTPException(422, "room_id is required")
    rows = await get_voice_notes(db, room_id)
    return [
        VoiceNoteResponse(
            id=r["id"], room_id=r["room_id"], alert_id=r["alert_id"],
            sender_role=r["sender_role"], sender_name=r["sender_name"],
            created_at=r["created_at"].isoformat() if hasattr(r["created_at"], "isoformat") else str(r["created_at"]),
            duration_ms=r["duration_ms"], language=r["language"] or "en",
            audio_url=f"/voice-notes/{r['id']}/audio",
            original_text=r.get("original_text"),
            translated_text=r.get("translated_text"),
            source_language=r.get("source_language"),
            target_language=r.get("target_language"),
        ) for r in rows
    ]


@router.get("/{note_id}/audio")
async def get_voice_note_audio(note_id: int, current_user: CurrentUser, db: asyncpg.Connection = Depends(get_db)):
    row = await get_voice_note(db, note_id)
    if row is None:
        raise HTTPException(404, "Voice note not found")
    path = VOICE_DIR / row["filename"]
    if not path.exists():
        raise HTTPException(404, "Voice note file is missing")
    return FileResponse(path, media_type=row["mime_type"], filename=row["filename"])
