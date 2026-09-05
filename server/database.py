"""
database.py — SQLite schema definition and async helper functions.

Schema
------
alerts
    id              INTEGER PRIMARY KEY AUTOINCREMENT
    room_id         TEXT    NOT NULL          -- e.g. "4B"
    priority        TEXT    NOT NULL          -- "Critical" | "Urgent" | "Routine"
    intent          TEXT    NOT NULL          -- one of 9 intent categories
    distress_score  REAL    NOT NULL          -- 0.0 – 1.0 fused urgency score
    transcript      TEXT    NOT NULL          -- STT output
    wav_path        TEXT                      -- absolute path to saved .wav (nullable)
    acknowledged    INTEGER NOT NULL DEFAULT 0   -- 0 = pending, 1 = ACK'd
    ack_by          TEXT                      -- nurse identifier who ACK'd (nullable)
    created_at      TEXT    NOT NULL          -- ISO-8601 UTC timestamp
    ack_at          TEXT                      -- ISO-8601 UTC timestamp (nullable)

All functions accept an aiosqlite.Connection that is opened once per request
via a FastAPI dependency (get_db), keeping connection management out of
business logic.
"""

import logging
from datetime import datetime, timezone
from typing import AsyncGenerator

import aiosqlite

from config import settings

logger = logging.getLogger(__name__)

# ── Schema ────────────────────────────────────────────────────────────────────

DDL = """
CREATE TABLE IF NOT EXISTS alerts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id        TEXT    NOT NULL,
    priority       TEXT    NOT NULL,
    intent         TEXT    NOT NULL,
    distress_score REAL    NOT NULL,
    transcript     TEXT    NOT NULL,
    wav_path       TEXT,
    acknowledged   INTEGER NOT NULL DEFAULT 0,
    ack_by         TEXT,
    created_at     TEXT    NOT NULL,
    ack_at         TEXT
);

-- Index used by GET /alerts/latest (ordered by created_at DESC, unACK'd first)
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_ack     ON alerts (acknowledged, created_at DESC);
"""


async def init_db() -> None:
    """
    Create the database file and run DDL statements.
    Called once from the FastAPI lifespan startup hook in main.py.
    Safe to call on every startup — all statements use IF NOT EXISTS.
    """
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await db.executescript(DDL)
        await db.commit()
    logger.info("Database initialised at %s", settings.DB_PATH)


# ── FastAPI dependency ────────────────────────────────────────────────────────

async def get_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    """
    Yields an open aiosqlite connection for the duration of a single request.

    Usage in a route:
        async def my_route(db: aiosqlite.Connection = Depends(get_db)):
            ...
    """
    async with aiosqlite.connect(settings.DB_PATH) as db:
        db.row_factory = aiosqlite.Row   # rows behave like dicts
        try:
            yield db
        except Exception:
            await db.rollback()
            raise
        else:
            await db.commit()


# ── Helper functions ──────────────────────────────────────────────────────────

async def insert_alert(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    priority: str,
    intent: str,
    distress_score: float,
    transcript: str,
    wav_path: str | None = None,
) -> int:
    """
    Insert a new alert row and return its auto-generated id.
    created_at is set to the current UTC time in ISO-8601 format.
    """
    now = utcnow()
    cursor = await db.execute(
        """
        INSERT INTO alerts
            (room_id, priority, intent, distress_score, transcript, wav_path, created_at)
        VALUES
            (?, ?, ?, ?, ?, ?, ?)
        """,
        (room_id, priority, intent, distress_score, transcript, wav_path, now),
    )
    alert_id = cursor.lastrowid
    logger.info(
        "Alert inserted: id=%d room=%s priority=%s intent=%s distress=%.2f",
        alert_id, room_id, priority, intent, distress_score,
    )
    return alert_id


async def get_latest_alerts(
    db: aiosqlite.Connection,
    *,
    limit: int = 20,
    unacked_only: bool = False,
) -> list[dict]:
    """
    Return the most recent alerts, newest first.

    Parameters
    ----------
    limit        : max number of rows to return (default 20)
    unacked_only : if True, exclude already-acknowledged alerts
    """
    if unacked_only:
        query = """
            SELECT * FROM alerts
            WHERE acknowledged = 0
            ORDER BY created_at DESC
            LIMIT ?
        """
    else:
        query = """
            SELECT * FROM alerts
            ORDER BY created_at DESC
            LIMIT ?
        """
    async with db.execute(query, (limit,)) as cursor:
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_alert_by_id(
    db: aiosqlite.Connection,
    alert_id: int,
) -> dict | None:
    """Return a single alert by primary key, or None if not found."""
    async with db.execute(
        "SELECT * FROM alerts WHERE id = ?", (alert_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return dict(row) if row else None


async def acknowledge_alert(
    db: aiosqlite.Connection,
    alert_id: int,
    *,
    ack_by: str,
) -> bool:
    """
    Mark an alert as acknowledged.

    Returns True if a row was updated, False if the alert_id did not exist
    or was already acknowledged.
    """
    now = utcnow()
    cursor = await db.execute(
        """
        UPDATE alerts
        SET acknowledged = 1,
            ack_by       = ?,
            ack_at       = ?
        WHERE id = ? AND acknowledged = 0
        """,
        (ack_by, now, alert_id),
    )
    updated = cursor.rowcount > 0
    if updated:
        logger.info("Alert %d acknowledged by %s at %s", alert_id, ack_by, now)
    else:
        logger.warning("ACK failed: alert %d not found or already acknowledged", alert_id)
    return updated


def utcnow() -> str:
    """Return the current UTC time as an ISO-8601 string with 'Z' suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
