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
    ack_at         TEXT,
    language       TEXT,                        -- detected/forced ISO lang: en|hi|kn|de (nullable)
    escalated      INTEGER NOT NULL DEFAULT 0,  -- 1 once auto-escalated Urgent→Critical
    escalated_at   TEXT,                        -- ISO-8601 UTC when escalation happened
    patient_name   TEXT,                        -- looked up from patients table (nullable)
    summary        TEXT,                        -- NLP request summary ("wants water")
    emotion        TEXT,                        -- calm|anxious|distressed|panicked
    alert_message  TEXT                         -- standardised message w/ prefix + summary
);

-- Index used by GET /alerts/latest (ordered by created_at DESC, unACK'd first)
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_ack     ON alerts (acknowledged, created_at DESC);
"""

# Columns added after the original release. SQLite has no "ADD COLUMN IF NOT
# EXISTS", so we attempt each ALTER and ignore the duplicate-column error. This
# keeps pre-existing databases working without a manual migration.
_MIGRATIONS = [
    # language stores a free-text ISO code: en|hi|kn|de (no CHECK constraint)
    "ALTER TABLE alerts ADD COLUMN language TEXT",
    "ALTER TABLE alerts ADD COLUMN escalated INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE alerts ADD COLUMN escalated_at TEXT",
    "ALTER TABLE alerts ADD COLUMN patient_name TEXT",
    "ALTER TABLE alerts ADD COLUMN summary TEXT",
    "ALTER TABLE alerts ADD COLUMN emotion TEXT",
    "ALTER TABLE alerts ADD COLUMN alert_message TEXT",
]


async def init_db() -> None:
    """
    Create the database file and run DDL statements.
    Called once from the FastAPI lifespan startup hook in main.py.
    Safe to call on every startup — all statements use IF NOT EXISTS.
    """
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await db.executescript(DDL)
        # Idempotent column migrations for databases created before these
        # columns existed. Duplicate-column errors are expected and ignored.
        for stmt in _MIGRATIONS:
            try:
                await db.execute(stmt)
            except Exception:
                pass  # column already exists
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
    language: str | None = None,
    patient_name: str | None = None,
    summary: str | None = None,
    emotion: str | None = None,
    alert_message: str | None = None,
) -> int:
    """
    Insert a new alert row and return its auto-generated id.
    created_at is set to the current UTC time in ISO-8601 format.
    """
    now = utcnow()
    cursor = await db.execute(
        """
        INSERT INTO alerts
            (room_id, priority, intent, distress_score, transcript, wav_path,
             created_at, language, patient_name, summary, emotion, alert_message)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (room_id, priority, intent, distress_score, transcript, wav_path, now,
         language, patient_name, summary, emotion, alert_message),
    )
    alert_id = cursor.lastrowid
    logger.info(
        "Alert inserted: id=%d room=%s priority=%s intent=%s emotion=%s",
        alert_id, room_id, priority, intent, emotion,
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


async def get_stale_unacked_urgent(
    db: aiosqlite.Connection,
    *,
    older_than_seconds: int,
) -> list[dict]:
    """
    Return unacknowledged Urgent alerts created more than `older_than_seconds`
    ago and not yet escalated. Used by the auto-escalation loop.
    """
    cutoff = (
        datetime.now(timezone.utc).timestamp() - older_than_seconds
    )
    query = """
        SELECT * FROM alerts
        WHERE acknowledged = 0
          AND priority = 'Urgent'
          AND escalated = 0
        ORDER BY created_at ASC
    """
    async with db.execute(query) as cursor:
        rows = await cursor.fetchall()

    stale: list[dict] = []
    for row in rows:
        d = dict(row)
        try:
            # created_at is ISO-8601 with a trailing 'Z'.
            ts = datetime.fromisoformat(d["created_at"].replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        if ts <= cutoff:
            stale.append(d)
    return stale


async def escalate_alert(db: aiosqlite.Connection, alert_id: int) -> bool:
    """
    Escalate an unacknowledged Urgent alert to Critical.

    Returns True if a row was changed. Guarded so it only affects alerts that
    are still Urgent, unacknowledged, and not already escalated (idempotent).
    """
    now = utcnow()
    cursor = await db.execute(
        """
        UPDATE alerts
        SET priority     = 'Critical',
            escalated    = 1,
            escalated_at = ?
        WHERE id = ? AND acknowledged = 0 AND priority = 'Urgent' AND escalated = 0
        """,
        (now, alert_id),
    )
    changed = cursor.rowcount > 0
    if changed:
        await db.commit()
        logger.info("Alert %d auto-escalated Urgent → Critical at %s", alert_id, now)
    return changed


def utcnow() -> str:
    """Return the current UTC time as an ISO-8601 string with 'Z' suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
