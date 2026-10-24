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
    alert_message  TEXT,                        -- standardised message w/ prefix + summary
    routed_to      TEXT,                        -- nurse username this alert was routed to (scheduler)
    fell_back      INTEGER NOT NULL DEFAULT 0,  -- 1 if no assigned nurse free -> broadcast fallback
    routed_at      TEXT,                        -- ISO-8601 UTC when last routed to a nurse (for reroute timeout)
    reroute_count  INTEGER NOT NULL DEFAULT 0   -- how many times auto-rerouted / manually redirected
);

-- Index used by GET /alerts/latest (ordered by created_at DESC, unACK'd first)
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_ack     ON alerts (acknowledged, created_at DESC);

-- ── Dispatch requests (Uber-style routing lifecycle) ───────────────────────
-- One row per alert that enters the dispatch loop. The alert itself (above)
-- stays the unit a nurse acknowledges; this table tracks WHO it is being
-- offered to and the state machine around primary → broadcast → admin.
--
-- state transitions:
--   pending_primary  → the patient's primary nurse was offered it
--   broadcasting     → primary declined/offline → offered to all FREE nurses
--   assigned         → a specific nurse claimed/was assigned it (assignee set)
--   needs_admin      → nobody free accepted within the broadcast window
--   resolved         → the underlying alert was acknowledged
--   cancelled        → superseded / alert gone
CREATE TABLE IF NOT EXISTS dispatch_requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id      INTEGER NOT NULL UNIQUE,
    room_id       TEXT    NOT NULL,
    primary_nurse TEXT,                       -- the patient's primary at dispatch time
    assignee      TEXT,                       -- nurse currently responsible (NULL while broadcasting)
    state         TEXT    NOT NULL DEFAULT 'pending_primary',
    decline_reason TEXT,                       -- reason the primary gave when declining
    broadcast_at  TEXT,                       -- ISO-8601 UTC when broadcast started (timeout anchor)
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dispatch_state ON dispatch_requests (state);
CREATE INDEX IF NOT EXISTS idx_dispatch_alert ON dispatch_requests (alert_id);

-- ── Department requests (canteen / pharmacy / housekeeping routing) ─────────
-- One row per NON-NURSE slice of a request routed by department_router.py.
-- Modeled on dispatch_requests but with its own acknowledge/escalate loop:
--   pending      → delivered to the department user, awaiting acknowledge
--   acknowledged → a department member took it
--   done         → completed
--   escalated    → SLA elapsed with no ack → handed to the nurse via dispatch.py
--   cancelled    → superseded / alert gone
-- Dedupe: repeat requests for the same (room_id, department) inside the window
-- increment repeat_count on the live row instead of inserting a duplicate.
CREATE TABLE IF NOT EXISTS department_requests (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id            INTEGER NOT NULL,
    department          TEXT    NOT NULL,          -- canteen | pharmacy | housekeeping
    target_username     TEXT,                      -- resolved assignee (dept account / floor mgr)
    room_id             TEXT    NOT NULL,
    floor               TEXT,                      -- room's floor (housekeeping routing)
    state               TEXT    NOT NULL DEFAULT 'pending',
    transcript          TEXT    NOT NULL,
    predicted_department TEXT   NOT NULL,          -- what the router predicted (audit)
    routing_reason      TEXT    NOT NULL,          -- intent_map | keyword | model
    confidence          REAL    NOT NULL DEFAULT 0.0,
    model_version       TEXT    NOT NULL DEFAULT '',
    shadow              INTEGER NOT NULL DEFAULT 1, -- 1 = would-route only, not actually dispatched
    repeat_count        INTEGER NOT NULL DEFAULT 0, -- deduped repeats merged into this row
    acknowledged_by     TEXT,
    created_at          TEXT    NOT NULL,
    acknowledged_at     TEXT,
    done_at             TEXT,
    escalated_at        TEXT,
    updated_at          TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deptreq_state ON department_requests (state);
CREATE INDEX IF NOT EXISTS idx_deptreq_alert ON department_requests (alert_id);
CREATE INDEX IF NOT EXISTS idx_deptreq_target ON department_requests (target_username, state);
CREATE INDEX IF NOT EXISTS idx_deptreq_dedupe ON department_requests (room_id, department, state);

-- Runtime department feature-flag overrides (admin toggles persist a restart).
CREATE TABLE IF NOT EXISTS department_flags (
    key        TEXT PRIMARY KEY,   -- e.g. 'enabled','shadow','canteen','pharmacy','housekeeping'
    value      INTEGER NOT NULL,   -- 0 | 1
    updated_at TEXT    NOT NULL
);
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
    "ALTER TABLE alerts ADD COLUMN routed_to TEXT",
    "ALTER TABLE alerts ADD COLUMN fell_back INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE alerts ADD COLUMN routed_at TEXT",
    "ALTER TABLE alerts ADD COLUMN reroute_count INTEGER NOT NULL DEFAULT 0",
    # Department-routing audit: what the router decided for this alert and why.
    # dept_routed_to = department(s) chosen, comma-joined; NULL/'nurse' = nurse.
    "ALTER TABLE alerts ADD COLUMN dept_routed_to TEXT",
    "ALTER TABLE alerts ADD COLUMN dept_confidence REAL",
    "ALTER TABLE alerts ADD COLUMN dept_reason TEXT",
    "ALTER TABLE alerts ADD COLUMN dept_model_version TEXT",
    "ALTER TABLE alerts ADD COLUMN dept_shadow INTEGER NOT NULL DEFAULT 0",
    # Marks an alert that reached the nurse as an ESCALATED department fallback
    # (SLA elapsed) so the nurse UI can render it distinctly from a fresh call.
    "ALTER TABLE alerts ADD COLUMN dept_escalated INTEGER NOT NULL DEFAULT 0",
]


async def init_db() -> None:
    """
    Create the database file and run DDL statements.
    Called once from the FastAPI lifespan startup hook in main.py.
    Safe to call on every startup — all statements use IF NOT EXISTS.
    """
    async with aiosqlite.connect(settings.DB_PATH) as db:
        # WAL + a busy timeout let multiple short-lived connections (one per
        # request, plus the dispatch timer workers) write concurrently without
        # "database is locked" errors — writers wait briefly instead of failing.
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
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
        await db.execute("PRAGMA busy_timeout=5000")
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


async def get_active_alert_counts_by_nurse(
    db: aiosqlite.Connection,
) -> dict[str, int]:
    """Return {nurse_username: count} of unacknowledged alerts currently routed
    to each nurse (ack_by is set at routing time, cleared is never — we count
    rows that were routed to a nurse but not yet acknowledged).

    Used by the scheduler's load-balancing tiebreak and the busy definition
    ("holding an unacknowledged alert").
    """
    query = """
        SELECT routed_to AS nurse, COUNT(*) AS n
        FROM alerts
        WHERE acknowledged = 0 AND routed_to IS NOT NULL
        GROUP BY routed_to
    """
    try:
        async with db.execute(query) as cursor:
            rows = await cursor.fetchall()
    except Exception:
        # routed_to column may not exist on a very old DB; treat as empty.
        return {}
    return {row["nurse"]: row["n"] for row in rows if row["nurse"]}


async def set_alert_routed_to(
    db: aiosqlite.Connection, alert_id: int, nurse_username: str | None, fell_back: bool
) -> None:
    """Record which nurse an alert was routed to (and whether it fell back to
    broadcast), stamping routed_at = now. Used for busy detection, the reroute
    timeout, and audit."""
    try:
        await db.execute(
            "UPDATE alerts SET routed_to = ?, fell_back = ?, routed_at = ? WHERE id = ?",
            (nurse_username, 1 if fell_back else 0, utcnow(), alert_id),
        )
        await db.commit()
    except Exception as exc:
        logger.warning("Could not set routed_to for alert %d: %s", alert_id, exc)


async def record_reroute(
    db: aiosqlite.Connection, alert_id: int, nurse_username: str | None, fell_back: bool
) -> None:
    """Like set_alert_routed_to but also increments reroute_count — used when an
    alert is auto-rerouted (no ack in time) or manually redirected."""
    try:
        await db.execute(
            """
            UPDATE alerts
            SET routed_to = ?, fell_back = ?, routed_at = ?, reroute_count = reroute_count + 1
            WHERE id = ?
            """,
            (nurse_username, 1 if fell_back else 0, utcnow(), alert_id),
        )
        await db.commit()
    except Exception as exc:
        logger.warning("Could not record reroute for alert %d: %s", alert_id, exc)


async def get_stale_routed_unacked(
    db: aiosqlite.Connection, *, older_than_seconds: int, max_reroutes: int = 5
) -> list[dict]:
    """Return alerts that were routed to a SPECIFIC nurse, are still unacked, and
    whose routed_at is older than the timeout — candidates for auto-reroute.

    Skips alerts that already fell back to broadcast (routed_to is NULL) and
    those that have been rerouted too many times (give up -> they remain
    broadcast-visible via /alerts/latest)."""
    cutoff = datetime.now(timezone.utc).timestamp() - older_than_seconds
    query = """
        SELECT * FROM alerts
        WHERE acknowledged = 0
          AND routed_to IS NOT NULL
          AND fell_back = 0
          AND reroute_count < ?
        ORDER BY created_at ASC
    """
    try:
        async with db.execute(query, (max_reroutes,)) as cursor:
            rows = await cursor.fetchall()
    except Exception:
        return []
    stale: list[dict] = []
    for row in rows:
        d = dict(row)
        ra = d.get("routed_at")
        if not ra:
            continue
        try:
            ts = datetime.fromisoformat(ra.replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        if ts <= cutoff:
            stale.append(d)
    return stale


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


# ── Dispatch request helpers ──────────────────────────────────────────────────
# These implement the durable, race-safe side of the Uber-style dispatch loop.
# The in-memory timers and WebSocket fan-out live in dispatch.py; this module
# only owns the authoritative state in SQLite so concurrent accepts resolve to
# exactly one winner and the admin board survives a reconnect/poll.

# Terminal states a request can no longer transition out of.
_DISPATCH_TERMINAL = ("resolved", "cancelled")


async def create_dispatch_request(
    db: aiosqlite.Connection,
    *,
    alert_id: int,
    room_id: str,
    primary_nurse: str | None,
    initial_state: str,
    assignee: str | None = None,
) -> int:
    """Create (or replace) the dispatch request for an alert. One per alert.

    If a row already exists for this alert (e.g. a duplicate route), it is reset
    to the new initial state so the loop never double-tracks the same alert.
    Returns the dispatch request id.
    """
    now = utcnow()
    broadcast_at = now if initial_state == "broadcasting" else None
    # alert_id is UNIQUE — upsert so a re-route of the same alert replaces state.
    await db.execute(
        """
        INSERT INTO dispatch_requests
            (alert_id, room_id, primary_nurse, assignee, state, broadcast_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(alert_id) DO UPDATE SET
            room_id       = excluded.room_id,
            primary_nurse = excluded.primary_nurse,
            assignee      = excluded.assignee,
            state         = excluded.state,
            decline_reason = NULL,
            broadcast_at  = excluded.broadcast_at,
            updated_at    = excluded.updated_at
        """,
        (alert_id, room_id, primary_nurse, assignee, initial_state, broadcast_at, now, now),
    )
    await db.commit()
    async with db.execute(
        "SELECT id FROM dispatch_requests WHERE alert_id = ?", (alert_id,)
    ) as cur:
        row = await cur.fetchone()
    return row["id"]


async def get_dispatch_request(db: aiosqlite.Connection, request_id: int) -> dict | None:
    async with db.execute(
        "SELECT * FROM dispatch_requests WHERE id = ?", (request_id,)
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def get_dispatch_by_alert(db: aiosqlite.Connection, alert_id: int) -> dict | None:
    async with db.execute(
        "SELECT * FROM dispatch_requests WHERE alert_id = ?", (alert_id,)
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def set_dispatch_broadcasting(
    db: aiosqlite.Connection, request_id: int, *, decline_reason: str | None = None
) -> dict | None:
    """Move a request into the broadcasting state (primary declined/offline).

    Guarded so only a non-terminal, not-yet-assigned-by-accept request can be
    broadcast. Stamps broadcast_at = now (the 10s timeout anchor). Returns the
    updated row, or None if the transition was not applied (already resolved,
    already assigned to someone who accepted, etc.).
    """
    now = utcnow()
    cur = await db.execute(
        """
        UPDATE dispatch_requests
        SET state = 'broadcasting',
            assignee = NULL,
            decline_reason = ?,
            broadcast_at = ?,
            updated_at = ?
        WHERE id = ?
          AND state IN ('pending_primary', 'needs_admin', 'assigned')
        """,
        (decline_reason, now, now, request_id),
    )
    await db.commit()
    if cur.rowcount == 0:
        return None
    return await get_dispatch_request(db, request_id)


async def claim_dispatch_request(
    db: aiosqlite.Connection, request_id: int, nurse: str
) -> tuple[bool, dict | None]:
    """Atomically claim a request for `nurse`.

    Exactly one concurrent caller wins: the UPDATE only succeeds when the row is
    still open (pending_primary / broadcasting / needs_admin) AND not already
    assigned to someone else. SQLite serialises writes, so two near-simultaneous
    accepts cannot both match. Returns (won, row).

    If the request is already assigned to THIS nurse, that is treated as an
    idempotent win (double-tap Accept) rather than a loss.
    """
    now = utcnow()
    cur = await db.execute(
        """
        UPDATE dispatch_requests
        SET state = 'assigned',
            assignee = ?,
            updated_at = ?
        WHERE id = ?
          AND state IN ('pending_primary', 'broadcasting', 'needs_admin')
          AND (assignee IS NULL OR assignee = ?)
        """,
        (nurse, now, request_id, nurse),
    )
    await db.commit()
    row = await get_dispatch_request(db, request_id)
    if cur.rowcount > 0:
        return True, row
    # Idempotent: already assigned to this same nurse.
    if row and row["state"] == "assigned" and row["assignee"] == nurse:
        return True, row
    return False, row


async def assign_dispatch_request(
    db: aiosqlite.Connection, request_id: int, nurse: str
) -> dict | None:
    """Admin override: force-assign a request to a specific nurse.

    Allowed from any non-terminal state. Returns the updated row, or None if the
    request is already resolved/cancelled."""
    now = utcnow()
    cur = await db.execute(
        """
        UPDATE dispatch_requests
        SET state = 'assigned',
            assignee = ?,
            updated_at = ?
        WHERE id = ?
          AND state NOT IN ('resolved', 'cancelled')
        """,
        (nurse, now, request_id),
    )
    await db.commit()
    if cur.rowcount == 0:
        return None
    return await get_dispatch_request(db, request_id)


async def mark_dispatch_needs_admin(db: aiosqlite.Connection, request_id: int) -> dict | None:
    """Move a still-broadcasting request to needs_admin (timeout with no taker).

    Guarded so an accept that landed first is never clobbered."""
    now = utcnow()
    cur = await db.execute(
        """
        UPDATE dispatch_requests
        SET state = 'needs_admin',
            assignee = NULL,
            updated_at = ?
        WHERE id = ?
          AND state = 'broadcasting'
        """,
        (now, request_id),
    )
    await db.commit()
    if cur.rowcount == 0:
        return None
    return await get_dispatch_request(db, request_id)


async def resolve_dispatch_for_alert(
    db: aiosqlite.Connection, alert_id: int, *, by_nurse: str | None = None
) -> dict | None:
    """Mark the dispatch request for an alert as resolved (alert acknowledged).

    Idempotent. Returns the row (whatever its final state) or None if none."""
    now = utcnow()
    await db.execute(
        """
        UPDATE dispatch_requests
        SET state = 'resolved',
            assignee = COALESCE(?, assignee),
            updated_at = ?
        WHERE alert_id = ?
          AND state NOT IN ('resolved', 'cancelled')
        """,
        (by_nurse, now, alert_id),
    )
    await db.commit()
    return await get_dispatch_by_alert(db, alert_id)


async def cancel_dispatch_request(db: aiosqlite.Connection, request_id: int) -> None:
    now = utcnow()
    await db.execute(
        """
        UPDATE dispatch_requests
        SET state = 'cancelled', updated_at = ?
        WHERE id = ? AND state NOT IN ('resolved', 'cancelled')
        """,
        (now, request_id),
    )
    await db.commit()


async def get_active_dispatch_requests(db: aiosqlite.Connection) -> list[dict]:
    """All non-terminal dispatch requests, newest first — for the admin board
    and for timer re-arming after a restart."""
    async with db.execute(
        """
        SELECT * FROM dispatch_requests
        WHERE state NOT IN ('resolved', 'cancelled')
        ORDER BY created_at DESC
        """
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def count_active_dispatch_by_nurse(db: aiosqlite.Connection) -> dict[str, int]:
    """{nurse: n} assigned-but-unresolved dispatch requests, for busy detection."""
    async with db.execute(
        """
        SELECT assignee AS nurse, COUNT(*) AS n
        FROM dispatch_requests
        WHERE state = 'assigned' AND assignee IS NOT NULL
        GROUP BY assignee
        """
    ) as cur:
        rows = await cur.fetchall()
    return {r["nurse"]: r["n"] for r in rows if r["nurse"]}


async def reset_all_dispatch(db: aiosqlite.Connection) -> int:
    """Cancel every non-terminal dispatch request. Returns the number cancelled.

    Used by the admin "Reset Nurses" action to clear stuck 'holding'/'assigned'
    state so all online nurses return to AVAILABLE. Does not touch the alerts
    themselves (unacknowledged alerts remain visible), only the dispatch holds.
    """
    now = utcnow()
    cur = await db.execute(
        """
        UPDATE dispatch_requests
        SET state = 'cancelled', assignee = NULL, updated_at = ?
        WHERE state NOT IN ('resolved', 'cancelled')
        """,
        (now,),
    )
    await db.commit()
    return cur.rowcount if cur.rowcount is not None else 0


async def release_all_routed_alerts(db: aiosqlite.Connection) -> int:
    """Clear routed_to on all UNACKNOWLEDGED alerts, so no nurse is counted as
    'holding' them. The alerts themselves stay (acknowledged=0, still visible as
    pending/unrouted); this just releases the per-nurse hold used by the busy
    calculation. Returns the number of alerts released.
    """
    cur = await db.execute(
        """
        UPDATE alerts
        SET routed_to = NULL, fell_back = 1
        WHERE acknowledged = 0 AND routed_to IS NOT NULL
        """
    )
    await db.commit()
    return cur.rowcount if cur.rowcount is not None else 0


# ── Department routing audit on the alert row ─────────────────────────────────

async def set_alert_department_routing(
    db: aiosqlite.Connection,
    alert_id: int,
    *,
    dept_routed_to: str | None,
    confidence: float | None,
    reason: str | None,
    model_version: str | None,
    shadow: bool,
) -> None:
    """Record the router's decision on the alert row for admin audit.

    dept_routed_to is a comma-joined list of non-nurse departments, or None/'nurse'
    when the request stayed with the nurse. This is write-only audit metadata and
    never changes the nurse dispatch path.
    """
    try:
        await db.execute(
            """
            UPDATE alerts
            SET dept_routed_to = ?, dept_confidence = ?, dept_reason = ?,
                dept_model_version = ?, dept_shadow = ?
            WHERE id = ?
            """,
            (dept_routed_to, confidence, reason, model_version, 1 if shadow else 0, alert_id),
        )
        await db.commit()
    except Exception as exc:
        logger.warning("Could not set department routing audit for alert %d: %s", alert_id, exc)


async def mark_alert_dept_escalated(db: aiosqlite.Connection, alert_id: int) -> None:
    """Flag that this alert reached the nurse as an escalated department fallback."""
    try:
        await db.execute(
            "UPDATE alerts SET dept_escalated = 1 WHERE id = ?", (alert_id,)
        )
        await db.commit()
    except Exception as exc:
        logger.warning("Could not mark alert %d dept_escalated: %s", alert_id, exc)


# ── Department request helpers ────────────────────────────────────────────────
# The durable side of department_dispatch.py's acknowledge/escalate loop. The
# in-memory SLA timers and WebSocket fan-out live in department_dispatch.py;
# this module owns the authoritative SQLite state.

_DEPT_TERMINAL = ("done", "escalated", "cancelled")


async def find_dedupe_department_request(
    db: aiosqlite.Connection, *, room_id: str, department: str, within_seconds: int
) -> dict | None:
    """Return a still-open (pending/acknowledged) request for the same room +
    department created within `within_seconds`, for dedupe. Newest first."""
    cutoff = datetime.now(timezone.utc).timestamp() - within_seconds
    async with db.execute(
        """
        SELECT * FROM department_requests
        WHERE room_id = ? AND department = ? AND state IN ('pending','acknowledged')
        ORDER BY created_at DESC
        """,
        (room_id, department),
    ) as cur:
        rows = await cur.fetchall()
    for row in rows:
        d = dict(row)
        try:
            ts = datetime.fromisoformat(d["created_at"].replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        if ts >= cutoff:
            return d
    return None


async def bump_department_repeat(db: aiosqlite.Connection, request_id: int) -> None:
    """Increment the repeat counter on a deduped department request."""
    now = utcnow()
    await db.execute(
        "UPDATE department_requests SET repeat_count = repeat_count + 1, updated_at = ? WHERE id = ?",
        (now, request_id),
    )
    await db.commit()


async def create_department_request(
    db: aiosqlite.Connection,
    *,
    alert_id: int,
    department: str,
    target_username: str | None,
    room_id: str,
    floor: str | None,
    transcript: str,
    predicted_department: str,
    routing_reason: str,
    confidence: float,
    model_version: str,
    shadow: bool,
    initial_state: str = "pending",
) -> int:
    """Create a department request row and return its id."""
    now = utcnow()
    cur = await db.execute(
        """
        INSERT INTO department_requests
            (alert_id, department, target_username, room_id, floor, state, transcript,
             predicted_department, routing_reason, confidence, model_version, shadow,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (alert_id, department, target_username, room_id, floor, initial_state, transcript,
         predicted_department, routing_reason, confidence, model_version, 1 if shadow else 0,
         now, now),
    )
    await db.commit()
    return cur.lastrowid


async def get_department_request(db: aiosqlite.Connection, request_id: int) -> dict | None:
    async with db.execute(
        "SELECT * FROM department_requests WHERE id = ?", (request_id,)
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def acknowledge_department_request(
    db: aiosqlite.Connection, request_id: int, by_username: str
) -> dict | None:
    """Mark a pending department request acknowledged. Atomic: only succeeds from
    'pending', so two dashboards can't both claim it. Returns the row on success,
    None if it was already taken / terminal."""
    now = utcnow()
    cur = await db.execute(
        """
        UPDATE department_requests
        SET state = 'acknowledged', acknowledged_by = ?, acknowledged_at = ?, updated_at = ?
        WHERE id = ? AND state = 'pending'
        """,
        (by_username, now, now, request_id),
    )
    await db.commit()
    if cur.rowcount == 0:
        return None
    return await get_department_request(db, request_id)


async def complete_department_request(
    db: aiosqlite.Connection, request_id: int, by_username: str | None = None
) -> dict | None:
    """Mark a department request done (from pending or acknowledged)."""
    now = utcnow()
    cur = await db.execute(
        """
        UPDATE department_requests
        SET state = 'done', done_at = ?, updated_at = ?,
            acknowledged_by = COALESCE(acknowledged_by, ?)
        WHERE id = ? AND state IN ('pending','acknowledged')
        """,
        (now, now, by_username, request_id),
    )
    await db.commit()
    if cur.rowcount == 0:
        return None
    return await get_department_request(db, request_id)


async def mark_department_escalated(db: aiosqlite.Connection, request_id: int) -> dict | None:
    """Move a still-open request to 'escalated' (SLA elapsed, handed to nurse).
    Guarded so an ack/done that landed first is never clobbered."""
    now = utcnow()
    cur = await db.execute(
        """
        UPDATE department_requests
        SET state = 'escalated', escalated_at = ?, updated_at = ?
        WHERE id = ? AND state = 'pending'
        """,
        (now, now, request_id),
    )
    await db.commit()
    if cur.rowcount == 0:
        return None
    return await get_department_request(db, request_id)


async def cancel_department_request(db: aiosqlite.Connection, request_id: int) -> None:
    now = utcnow()
    await db.execute(
        """
        UPDATE department_requests
        SET state = 'cancelled', updated_at = ?
        WHERE id = ? AND state NOT IN ('done','escalated','cancelled')
        """,
        (now, request_id),
    )
    await db.commit()


async def list_department_requests_for_user(
    db: aiosqlite.Connection, username: str, *, include_done: bool = False, limit: int = 100
) -> list[dict]:
    """Requests assigned to one department user (their dashboard queue)."""
    if include_done:
        query = """
            SELECT * FROM department_requests
            WHERE target_username = ?
            ORDER BY created_at DESC LIMIT ?
        """
    else:
        query = """
            SELECT * FROM department_requests
            WHERE target_username = ? AND state IN ('pending','acknowledged')
            ORDER BY created_at DESC LIMIT ?
        """
    async with db.execute(query, (username, limit)) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_active_department_requests(db: aiosqlite.Connection) -> list[dict]:
    """All non-terminal, non-shadow department requests — for SLA timer re-arm."""
    async with db.execute(
        """
        SELECT * FROM department_requests
        WHERE state = 'pending' AND shadow = 0
        ORDER BY created_at ASC
        """
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ── Department feature-flag persistence (admin runtime toggles) ───────────────

async def get_department_flags(db: aiosqlite.Connection) -> dict[str, bool]:
    """Return any admin-persisted department flag overrides as {key: bool}."""
    try:
        async with db.execute("SELECT key, value FROM department_flags") as cur:
            rows = await cur.fetchall()
    except Exception:
        return {}
    return {r["key"]: bool(r["value"]) for r in rows}


async def set_department_flag(db: aiosqlite.Connection, key: str, value: bool) -> None:
    now = utcnow()
    await db.execute(
        """
        INSERT INTO department_flags (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, 1 if value else 0, now),
    )
    await db.commit()
