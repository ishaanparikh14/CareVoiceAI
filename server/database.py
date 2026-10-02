"""PostgreSQL-only persistence layer for alerts and voice notes.

This module intentionally contains NO SQLite/aiosqlite code.  It keeps the
existing function names used by the routers so the rest of CareVoice can move
to PostgreSQL without changing every call site at once.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

import asyncpg

from config import settings
from models import Priority
from pg_database import get_conn

# Backwards-compatible dependency name used by existing routers.
get_db = get_conn


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_value(row: asyncpg.Record, key: str, default=None):
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


async def _patient_name_for_room(conn: asyncpg.Connection, room_id: str) -> str:
    return str(await conn.fetchval(
        """SELECT full_name FROM patients
           WHERE room_number=$1 AND is_discharged=FALSE
           ORDER BY id LIMIT 1""", room_id
    ) or "Patient")


async def insert_alert(
    conn: asyncpg.Connection,
    *,
    room_id: str,
    priority: str,
    intent: str,
    distress_score: float = 0.0,
    transcript: str,
    wav_path: str | None = None,
    patient_name: str | None = None,
    language: str = "en",
    nlp_summary: str = "",
) -> int:
    now = datetime.now(timezone.utc)
    patient_name = patient_name or await _patient_name_for_room(conn, room_id)
    initial_priority = priority

    escalation_deadline = None
    if priority == Priority.URGENT.value:
        escalation_deadline = now + timedelta(
            seconds=settings.URGENT_ESCALATION_TIMEOUT_SECONDS
        )

    return int(await conn.fetchval(
        """
        INSERT INTO alerts
            (room_id, patient_name, language, priority, initial_priority, intent,
             distress_score, transcript, wav_path, acknowledged, attended,
             escalation_deadline, escalation_count, created_at, nlp_summary)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,FALSE,FALSE,$10,0,$11,$12)
        RETURNING id
        """,
        room_id, patient_name, language, priority, initial_priority, intent,
        float(distress_score), transcript, wav_path, escalation_deadline, now,
        nlp_summary or "",
    ))


async def get_alert_by_id(conn: asyncpg.Connection, alert_id: int):
    return await conn.fetchrow("SELECT * FROM alerts WHERE id=$1", alert_id)


async def get_latest_alerts(
    conn: asyncpg.Connection, limit: int = 20, unacked_only: bool = False
):
    if unacked_only:
        return await conn.fetch(
            "SELECT * FROM alerts WHERE acknowledged=FALSE ORDER BY created_at DESC LIMIT $1",
            limit,
        )
    return await conn.fetch(
        "SELECT * FROM alerts ORDER BY created_at DESC LIMIT $1", limit
    )


async def acknowledge_alert(conn: asyncpg.Connection, alert_id: int, ack_by: str) -> bool:
    result = await conn.execute(
        """UPDATE alerts SET acknowledged=TRUE, ack_by=$1, ack_at=NOW()
           WHERE id=$2 AND acknowledged=FALSE""", ack_by, alert_id
    )
    return result == "UPDATE 1"


async def attend_alert(conn: asyncpg.Connection, alert_id: int, attended_by: str) -> bool:
    result = await conn.execute(
        """UPDATE alerts SET attended=TRUE, attended_by=$1, attended_at=NOW()
           WHERE id=$2 AND attended=FALSE""", attended_by, alert_id
    )
    return result == "UPDATE 1"


async def update_alert_priority_if_expired(conn: asyncpg.Connection, alert_id: int) -> bool:
    result = await conn.execute(
        """UPDATE alerts
           SET priority=$1, escalated_at=NOW(), escalation_count=escalation_count+1
           WHERE id=$2 AND priority=$3 AND attended=FALSE
             AND escalated_at IS NULL AND escalation_deadline IS NOT NULL
             AND escalation_deadline <= NOW()""",
        Priority.CRITICAL.value, alert_id, Priority.URGENT.value,
    )
    return result == "UPDATE 1"


async def get_expired_urgent_ids(conn: asyncpg.Connection) -> list[int]:
    rows = await conn.fetch(
        """SELECT id FROM alerts
           WHERE priority=$1 AND attended=FALSE AND escalated_at IS NULL
             AND escalation_deadline IS NOT NULL AND escalation_deadline <= NOW()""",
        Priority.URGENT.value,
    )
    return [int(r["id"]) for r in rows]


async def create_voice_note(
    conn: asyncpg.Connection,
    *,
    room_id: str,
    alert_id: int | None,
    sender_user_id: int,
    sender_role: str,
    sender_name: str,
    filename: str,
    mime_type: str,
    duration_ms: int,
    language: str = "en",
    original_text: str | None = None,
    translated_text: str | None = None,
    source_language: str | None = None,
    target_language: str | None = None,
) -> int:
    return int(await conn.fetchval(
        """INSERT INTO voice_notes
           (room_id, alert_id, sender_user_id, sender_role, sender_name,
            filename, mime_type, duration_ms, language,
            original_text, translated_text, source_language, target_language, created_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,NOW()) RETURNING id""",
        room_id, alert_id, sender_user_id, sender_role, sender_name,
        filename, mime_type, duration_ms, language,
        original_text, translated_text, source_language, target_language,
    ))


async def get_voice_notes(conn: asyncpg.Connection, room_id: str, limit: int = 20):
    return await conn.fetch(
        """SELECT id, room_id, alert_id, sender_role, sender_name,
                  created_at, duration_ms, language, filename, mime_type,
                  original_text, translated_text, source_language, target_language
           FROM voice_notes WHERE room_id=$1 ORDER BY created_at DESC LIMIT $2""",
        room_id, limit,
    )


async def get_voice_note(conn: asyncpg.Connection, note_id: int):
    return await conn.fetchrow("SELECT * FROM voice_notes WHERE id=$1", note_id)


def alert_response_dict(row: asyncpg.Record) -> dict[str, Any]:
    d = dict(row)
    d["created_at"] = _iso(d.get("created_at"))
    d["ack_at"] = _iso(d.get("ack_at"))
    d["attended_at"] = _iso(d.get("attended_at"))
    d["escalation_deadline"] = _iso(d.get("escalation_deadline"))
    d["escalated_at"] = _iso(d.get("escalated_at"))
    return d
