"""
pg_database.py — PostgreSQL schema, connection pool, and seed data.

Tables
------
users
    id            SERIAL PRIMARY KEY
    username      TEXT  UNIQUE NOT NULL        -- login name
    password_hash TEXT  NOT NULL               -- bcrypt hash
    full_name     TEXT  NOT NULL
    role          TEXT  NOT NULL               -- 'nurse' | 'patient'
    ward          TEXT                         -- e.g. "Ward A"  (nurses)
    is_active     BOOL  NOT NULL DEFAULT TRUE
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()

patients
    id            SERIAL PRIMARY KEY
    user_id       INT   REFERENCES users(id) ON DELETE CASCADE
    room_number   TEXT  NOT NULL               -- e.g. "4B"
    full_name     TEXT  NOT NULL
    age           INT
    diagnosis     TEXT
    attending     TEXT                         -- attending nurse username
    admitted_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
    is_discharged BOOL  NOT NULL DEFAULT FALSE

Sample data (seeded on first startup)
--------------------------------------
Nurses  : nurse_anna / pass123,  nurse_ben / pass123,  nurse_carol / pass123
Patients: patient_raj / pass123  (Room 4B),  patient_sara / pass123 (Room 4C),
          patient_tom / pass123  (Room 5A),  patient_lima / pass123 (Room 5B),
          patient_zoe / pass123  (Room 6A)
"""

import logging
from typing import AsyncGenerator

import asyncpg

from config import settings

logger = logging.getLogger(__name__)

# ── Module-level connection pool ──────────────────────────────────────────────
# Created once during FastAPI lifespan startup, shared across all requests.
_pool: asyncpg.Pool | None = None


async def create_pool() -> None:
    """Open the asyncpg connection pool. Called from main.py lifespan."""
    global _pool
    # Managed Postgres (Neon) requires TLS. Since sslmode is stripped from the
    # DSN for asyncpg, enable SSL explicitly here when the URL indicates it.
    ssl_arg = "require" if settings.DB_REQUIRES_SSL else None
    _pool = await asyncpg.create_pool(
        dsn      = settings.DATABASE_URL,
        min_size = 2,
        max_size = 10,
        ssl      = ssl_arg,
    )
    logger.info("PostgreSQL pool opened (ssl=%s)", bool(ssl_arg))


async def close_pool() -> None:
    """Close the pool gracefully on shutdown."""
    global _pool
    if _pool:
        await _pool.close()
        logger.info("PostgreSQL pool closed")


async def get_conn() -> AsyncGenerator[asyncpg.Connection, None]:
    """
    FastAPI dependency — yields a checked-out connection for one request.

    Usage:
        async def my_route(conn: asyncpg.Connection = Depends(get_conn)):
    """
    assert _pool is not None, "Pool not initialised — was create_pool() called?"
    async with _pool.acquire() as conn:
        yield conn


# ── DDL ───────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id              SERIAL PRIMARY KEY,
    username        TEXT  NOT NULL UNIQUE,
    password_hash   TEXT  NOT NULL,
    full_name       TEXT  NOT NULL,
    role            TEXT  NOT NULL CHECK (role IN ('nurse','patient','admin')),
    ward            TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    approval_status TEXT NOT NULL DEFAULT 'approved'
                    CHECK (approval_status IN ('pending','approved','rejected')),
    -- Routing attributes (nurses): skills, live status, supervisor flag.
    competencies    TEXT NOT NULL DEFAULT '',            -- comma-separated skills
    status          TEXT NOT NULL DEFAULT 'available',   -- available|in_patient_room|on_break|off_duty
    is_supervisor   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS patients (
    id            SERIAL PRIMARY KEY,
    user_id       INT  REFERENCES users(id) ON DELETE CASCADE,
    room_number   TEXT NOT NULL,
    full_name     TEXT NOT NULL,
    age           INT,
    diagnosis     TEXT,
    attending     TEXT,
    acuity        INT,                                   -- ESI 1 (most acute) – 5
    admitted_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_discharged BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS rooms (
    id           SERIAL PRIMARY KEY,
    room_number  TEXT NOT NULL UNIQUE,
    ward         TEXT,
    status       TEXT NOT NULL DEFAULT 'unoccupied'
                 CHECK (status IN ('occupied','unoccupied','cleaning','maintenance')),
    notes        TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Bidirectional nurse/patient voice notes attached to a room (and optionally an
-- alert). The audio file itself lives on disk under STORAGE_DIR/voice_notes;
-- this table stores the metadata plus the Whisper transcript (original_text).
CREATE TABLE IF NOT EXISTS voice_notes (
    id             SERIAL PRIMARY KEY,
    room_id        TEXT NOT NULL,
    alert_id       INT,
    sender_user_id INT  REFERENCES users(id) ON DELETE SET NULL,
    sender_role    TEXT NOT NULL,            -- 'nurse' | 'patient'
    sender_name    TEXT NOT NULL,
    filename       TEXT NOT NULL,            -- file on disk under STORAGE_DIR/voice_notes
    mime_type      TEXT NOT NULL DEFAULT 'audio/wav',
    duration_ms    INT  NOT NULL DEFAULT 0,
    language       TEXT NOT NULL DEFAULT 'en',  -- 'en' | 'hi'
    original_text  TEXT,                      -- Whisper transcript of the note
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index for fast login lookup
CREATE INDEX IF NOT EXISTS idx_users_username ON users (username);
-- Index for room-number lookups from patient app
CREATE INDEX IF NOT EXISTS idx_patients_room  ON patients (room_number);
-- Index for joining patient → user
CREATE INDEX IF NOT EXISTS idx_patients_user  ON patients (user_id);
-- Index for room lookups
CREATE INDEX IF NOT EXISTS idx_rooms_number    ON rooms (room_number);
-- Index for listing a room's voice notes newest-first
CREATE INDEX IF NOT EXISTS idx_voice_notes_room ON voice_notes (room_id, created_at DESC);

-- Many-to-many patient↔nurse assignment for scheduling/failover alert routing.
-- A patient (identified by room_number, matching how alerts are keyed) may have
-- several nurses. priority_order 0 = primary (kept in sync with patients.attending
-- for back-compat); lower numbers are tried first by the scheduler.
CREATE TABLE IF NOT EXISTS patient_nurses (
    id             SERIAL PRIMARY KEY,
    room_number    TEXT NOT NULL,
    nurse_username TEXT NOT NULL,
    priority_order INT  NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (room_number, nurse_username)
);
CREATE INDEX IF NOT EXISTS idx_patient_nurses_room ON patient_nurses (room_number, priority_order);
"""


# ── Seed data ─────────────────────────────────────────────────────────────────

# Passwords are bcrypt-hashed at seed time (see _seed() below).
# Plain-text for reference only — never stored.
_SEED_ADMINS = [
    # (username,  plain_pw,      full_name)
    ("admin",   "admin1234",  "Hospital Administrator"),
]

_SEED_NURSES = [
    # (username,      plain_pw,  full_name,          ward)
    ("nurse_anna",  "pass123", "Anna Reyes RN",     "Ward A"),
    ("nurse_ben",   "pass123", "Benjamin Okafor RN","Ward A"),
    ("nurse_carol", "pass123", "Carol Singh RN",    "Ward B"),
]

_SEED_PATIENTS = [
    # (username,       plain_pw,  full_name,           room,  age, diagnosis,              attending)
    ("patient_raj",  "pass123", "Rajesh Kumar",       "4B",  58,  "Post-op recovery",     "nurse_anna"),
    ("patient_sara", "pass123", "Sara Mendes",        "4C",  34,  "Fractured tibia",      "nurse_anna"),
    ("patient_tom",  "pass123", "Thomas Williams",    "5A",  72,  "Hypertensive crisis",  "nurse_ben"),
    ("patient_lima", "pass123", "Lima Patel",         "5B",  45,  "Appendectomy",         "nurse_ben"),
    ("patient_zoe",  "pass123", "Zoe Nakamura",       "6A",  29,  "Asthma exacerbation",  "nurse_carol"),
]


async def init_pg_db() -> None:
    """
    Create tables (idempotent) and seed sample data on first run.
    Called once from main.py lifespan startup.
    """
    assert _pool is not None
    async with _pool.acquire() as conn:
        # Run DDL
        await conn.execute(_DDL)
        logger.info("PostgreSQL schema ensured")

        # Migrate: drop old role CHECK constraint and replace with one that
        # includes 'admin'.  Safe to run repeatedly — does nothing if the
        # constraint already allows 'admin'.
        await _migrate_role_constraint(conn)

        # Migrate: add approval_status column to older databases.
        await _migrate_approval_status(conn)

        # Migrate: add routing columns (competencies/status/supervisor/acuity).
        await _migrate_routing_columns(conn)

        # Ensure at least one admin exists regardless of seed state
        admin_count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE role='admin'")
        if admin_count == 0:
            await _seed_admins(conn)

        # Check whether we need to seed nurses/patients
        count = await conn.fetchval("SELECT COUNT(*) FROM users")
        if count > 1:  # >1 because admin was just seeded above if missing
            logger.info("Seed data already present (%d users) — skipping nurses/patients", count)
        else:
            await _seed(conn)

        # Sync the rooms table with any room_numbers referenced by patients.
        # This runs every startup so newly-admitted rooms always appear.
        await _sync_rooms(conn)

        # Backfill the many-to-many patient_nurses table from attending, and
        # seed the canonical multi-nurse scenario for scheduling/failover.
        await _sync_patient_nurses(conn)


async def _migrate_approval_status(conn: asyncpg.Connection) -> None:
    """Add the approval_status column to legacy users tables (idempotent)."""
    await conn.execute(
        """
        ALTER TABLE users
        ADD COLUMN IF NOT EXISTS approval_status TEXT NOT NULL DEFAULT 'approved'
        """
    )
    # Ensure the CHECK constraint exists (only add if missing)
    exists = await conn.fetchval(
        """
        SELECT 1 FROM pg_constraint
        WHERE conname = 'users_approval_status_check'
        """
    )
    if not exists:
        try:
            await conn.execute(
                "ALTER TABLE users ADD CONSTRAINT users_approval_status_check "
                "CHECK (approval_status IN ('pending','approved','rejected'))"
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Could not add approval_status constraint: %s", exc)


async def _sync_rooms(conn: asyncpg.Connection) -> None:
    """
    Ensure every room_number referenced by a non-discharged patient exists in
    the rooms table, and set its status to 'occupied'. Rooms with no active
    patient keep their manually-set status (or default 'unoccupied').
    """
    # Insert any missing rooms (mark occupied since a patient references them)
    await conn.execute(
        """
        INSERT INTO rooms (room_number, status)
        SELECT DISTINCT room_number, 'occupied'
        FROM patients
        WHERE is_discharged = FALSE
        ON CONFLICT (room_number) DO NOTHING
        """
    )
    logger.info("Rooms table synced with active patients")


async def _migrate_role_constraint(conn: asyncpg.Connection) -> None:
    """
    Ensure the users.role CHECK constraint includes 'admin'.
    Drops the old constraint (if it exists and excludes 'admin') and recreates it.
    """
    # Find the constraint name for the role column check on the users table
    row = await conn.fetchrow(
        """
        SELECT conname
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE t.relname = 'users'
          AND c.contype = 'c'
          AND pg_get_constraintdef(c.oid) LIKE '%role%'
        """
    )
    if row:
        conname = row["conname"]
        # Check if 'admin' is already allowed
        defn = await conn.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = $1", conname
        )
        if defn and "admin" not in defn:
            await conn.execute(f'ALTER TABLE users DROP CONSTRAINT "{conname}"')
            await conn.execute(
                "ALTER TABLE users ADD CONSTRAINT users_role_check "
                "CHECK (role IN ('nurse','patient','admin'))"
            )
            logger.info("Migrated users.role CHECK constraint to include 'admin'")


async def _migrate_routing_columns(conn: asyncpg.Connection) -> None:
    """Add competency/status/supervisor/acuity columns to legacy DBs (idempotent),
    then seed sensible demo values for the sample staff/patients so the routing
    features are visible without manual data entry."""
    await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS competencies TEXT NOT NULL DEFAULT ''")
    await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'available'")
    await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_supervisor BOOLEAN NOT NULL DEFAULT FALSE")
    await conn.execute("ALTER TABLE patients ADD COLUMN IF NOT EXISTS acuity INT")

    # Demo competencies / supervisor so the scheme is visible out of the box.
    # Only applied where still at defaults (won't clobber real edits).
    demo = [
        # username,       competencies,                              supervisor
        ("nurse_anna",  "icu_certified,iv_start,medication_qualified", True),
        ("nurse_ben",   "iv_start,medication_qualified",              False),
        ("nurse_carol", "medication_qualified",                       False),
    ]
    for username, comps, supervisor in demo:
        await conn.execute(
            """
            UPDATE users
            SET competencies = $2,
                is_supervisor = $3
            WHERE username = $1 AND role = 'nurse' AND competencies = ''
            """,
            username, comps, supervisor,
        )

    # Demo acuity (ESI) for seeded patients where unset.
    acuity = [("4B", 2), ("4C", 3), ("5A", 1), ("5B", 4), ("6A", 3)]
    for room, esi in acuity:
        await conn.execute(
            "UPDATE patients SET acuity = $2 WHERE room_number = $1 AND acuity IS NULL",
            room, esi,
        )
    logger.info("Routing columns ensured (competencies/status/supervisor/acuity)")


async def _seed_admins(conn: asyncpg.Connection) -> None:
    """Insert admin accounts. Called whenever no admin exists in the DB."""
    from passlib.context import CryptContext
    pwd_ctx = CryptContext(schemes=["argon2"], deprecated="auto")

    for username, plain_pw, full_name in _SEED_ADMINS:
        hashed = pwd_ctx.hash(plain_pw)
        await conn.execute(
            """
            INSERT INTO users (username, password_hash, full_name, role)
            VALUES ($1, $2, $3, 'admin')
            ON CONFLICT (username) DO NOTHING
            """,
            username, hashed, full_name,
        )
        logger.info("Admin account ensured: %s", username)


async def _seed(conn: asyncpg.Connection) -> None:
    """Insert sample nurses and patients. Runs only when users table is empty."""
    from passlib.context import CryptContext  # local import — only needed at seed time
    pwd_ctx = CryptContext(schemes=["argon2"], deprecated="auto")

    logger.info("Seeding sample users and patients…")

    # ── Nurses ────────────────────────────────────────────────────────────────
    for username, plain_pw, full_name, ward in _SEED_NURSES:
        hashed = pwd_ctx.hash(plain_pw)
        await conn.execute(
            """
            INSERT INTO users (username, password_hash, full_name, role, ward)
            VALUES ($1, $2, $3, 'nurse', $4)
            ON CONFLICT (username) DO NOTHING
            """,
            username, hashed, full_name, ward,
        )
        logger.info("  Nurse seeded: %s (%s)", username, full_name)

    # ── Patients ──────────────────────────────────────────────────────────────
    for username, plain_pw, full_name, room, age, diagnosis, attending in _SEED_PATIENTS:
        hashed = pwd_ctx.hash(plain_pw)

        # Insert user row first
        user_id = await conn.fetchval(
            """
            INSERT INTO users (username, password_hash, full_name, role)
            VALUES ($1, $2, $3, 'patient')
            ON CONFLICT (username) DO NOTHING
            RETURNING id
            """,
            username, hashed, full_name,
        )

        if user_id:
            # Insert matching patient record
            await conn.execute(
                """
                INSERT INTO patients
                    (user_id, room_number, full_name, age, diagnosis, attending)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                user_id, room, full_name, age, diagnosis, attending,
            )
            logger.info("  Patient seeded: %s → Room %s", username, room)

    logger.info("Seed complete — %d nurses, %d patients",
                len(_SEED_NURSES), len(_SEED_PATIENTS))


# ── Registration helpers ──────────────────────────────────────────────────────

async def username_exists(conn: asyncpg.Connection, username: str) -> bool:
    """Return True if a user with this username already exists."""
    row = await conn.fetchval(
        "SELECT 1 FROM users WHERE username = $1", username
    )
    return row is not None


async def create_nurse(
    conn:       asyncpg.Connection,
    username:   str,
    password_hash: str,
    full_name:  str,
    ward:       str,
) -> int:
    """Insert a new nurse user (pending admin approval). Returns the new user id."""
    return await conn.fetchval(
        """
        INSERT INTO users (username, password_hash, full_name, role, ward, approval_status)
        VALUES ($1, $2, $3, 'nurse', $4, 'pending')
        RETURNING id
        """,
        username, password_hash, full_name, ward,
    )


async def create_patient(
    conn:          asyncpg.Connection,
    username:      str,
    password_hash: str,
    full_name:     str,
    room_number:   str,
    age:           int | None,
    diagnosis:     str | None,
    attending:     str | None,
) -> int:
    """Insert a new patient user + patient record. Returns the new user id."""
    user_id = await conn.fetchval(
        """
        INSERT INTO users (username, password_hash, full_name, role)
        VALUES ($1, $2, $3, 'patient')
        RETURNING id
        """,
        username, password_hash, full_name,
    )
    await conn.execute(
        """
        INSERT INTO patients
            (user_id, room_number, full_name, age, diagnosis, attending)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        user_id, room_number, full_name, age, diagnosis, attending,
    )
    return user_id


# ── Query helpers (used by auth + routers) ────────────────────────────────────

async def get_user_by_username(conn: asyncpg.Connection, username: str) -> asyncpg.Record | None:
    """Return the full users row for a given username, or None."""
    return await conn.fetchrow(
        "SELECT * FROM users WHERE username = $1 AND is_active = TRUE",
        username,
    )


async def get_patient_by_user_id(conn: asyncpg.Connection, user_id: int) -> asyncpg.Record | None:
    """Return the patients row linked to a user_id, or None."""
    return await conn.fetchrow(
        "SELECT * FROM patients WHERE user_id = $1 AND is_discharged = FALSE",
        user_id,
    )


async def get_patient_name_by_room(room_number: str) -> str | None:
    """
    Resolve the active patient's full name for a given room, acquiring its own
    connection from the pool. Used by the alert pipeline (which otherwise only
    touches SQLite) to build the standardised "Patient X in Room Y" message.
    Returns None if the pool isn't ready or no active patient is in that room.
    """
    if _pool is None or not room_number:
        return None
    try:
        async with _pool.acquire() as conn:
            return await conn.fetchval(
                """
                SELECT full_name FROM patients
                WHERE room_number = $1 AND is_discharged = FALSE
                ORDER BY admitted_at DESC
                LIMIT 1
                """,
                room_number.strip(),
            )
    except Exception:
        return None


async def update_user_profile(
    conn:       asyncpg.Connection,
    user_id:    int,
    full_name:  str | None = None,
    ward:       str | None = None,
) -> None:
    """Update mutable profile fields on the users row."""
    if full_name is not None:
        await conn.execute(
            "UPDATE users SET full_name = $1 WHERE id = $2",
            full_name, user_id,
        )
    if ward is not None:
        await conn.execute(
            "UPDATE users SET ward = $1 WHERE id = $2",
            ward, user_id,
        )


async def update_patient_profile(
    conn:        asyncpg.Connection,
    user_id:     int,
    full_name:   str | None = None,
    room_number: str | None = None,
    age:         int | None = None,
    diagnosis:   str | None = None,
    attending:   str | None = None,
) -> None:
    """Update mutable patient-specific fields. Also syncs full_name on patients row."""
    if full_name is not None:
        await conn.execute(
            "UPDATE patients SET full_name = $1 WHERE user_id = $2",
            full_name, user_id,
        )
    if room_number is not None:
        await conn.execute(
            "UPDATE patients SET room_number = $1 WHERE user_id = $2",
            room_number, user_id,
        )
    if age is not None:
        await conn.execute(
            "UPDATE patients SET age = $1 WHERE user_id = $2",
            age, user_id,
        )
    if diagnosis is not None:
        await conn.execute(
            "UPDATE patients SET diagnosis = $1 WHERE user_id = $2",
            diagnosis, user_id,
        )
    if attending is not None:
        await conn.execute(
            "UPDATE patients SET attending = $1 WHERE user_id = $2",
            attending, user_id,
        )


async def get_all_patients(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    """Return all non-discharged patients with their user info (for nurse dashboard)."""
    return await conn.fetch(
        """
        SELECT
            p.id, p.room_number, p.full_name, p.age, p.diagnosis,
            p.attending, p.admitted_at,
            u.username, u.is_active
        FROM patients p
        JOIN users u ON u.id = p.user_id
        WHERE p.is_discharged = FALSE
        ORDER BY p.room_number
        """
    )


async def get_patients_for_nurse(conn: asyncpg.Connection, nurse_username: str) -> list[asyncpg.Record]:
    """Return only the non-discharged patients assigned to a specific nurse."""
    return await conn.fetch(
        """
        SELECT
            p.id, p.room_number, p.full_name, p.age, p.diagnosis,
            p.attending, p.admitted_at,
            u.username, u.is_active
        FROM patients p
        JOIN users u ON u.id = p.user_id
        WHERE p.is_discharged = FALSE
          AND p.attending = $1
        ORDER BY p.room_number
        """,
        nurse_username,
    )


# ── Admin query helpers ───────────────────────────────────────────────────────

async def get_all_nurses(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    """Return all nurse users ordered by ward then name."""
    return await conn.fetch(
        """
        SELECT id, username, full_name, ward, is_active, created_at
        FROM users
        WHERE role = 'nurse'
        ORDER BY ward NULLS LAST, full_name
        """
    )


async def get_all_patients_admin(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    """Return all patients (including discharged) with user info for admin view."""
    return await conn.fetch(
        """
        SELECT
            p.id, p.room_number, p.full_name, p.age, p.diagnosis,
            p.attending, p.admitted_at, p.is_discharged,
            u.username, u.id AS user_id, u.is_active, u.created_at AS registered_at
        FROM patients p
        JOIN users u ON u.id = p.user_id
        ORDER BY p.is_discharged ASC, p.room_number
        """
    )


async def get_room_overview(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    """
    Return one row per occupied room with the patient name, diagnosis,
    and attending nurse name. Active (non-discharged) patients only.
    """
    return await conn.fetch(
        """
        SELECT
            p.room_number,
            p.full_name  AS patient_name,
            p.diagnosis,
            p.attending  AS attending_username,
            u2.full_name AS attending_name,
            u2.ward      AS ward,
            p.admitted_at
        FROM patients p
        JOIN users u  ON u.id  = p.user_id
        LEFT JOIN users u2 ON u2.username = p.attending AND u2.role = 'nurse'
        WHERE p.is_discharged = FALSE
        ORDER BY p.room_number
        """
    )


async def get_admin_stats(conn: asyncpg.Connection) -> dict:
    """Return aggregate counts for the admin overview cards."""
    nurses   = await conn.fetchval("SELECT COUNT(*) FROM users WHERE role='nurse' AND is_active=TRUE")
    patients = await conn.fetchval("SELECT COUNT(*) FROM patients WHERE is_discharged=FALSE")
    discharged = await conn.fetchval("SELECT COUNT(*) FROM patients WHERE is_discharged=TRUE")
    rooms    = await conn.fetchval(
        "SELECT COUNT(DISTINCT room_number) FROM patients WHERE is_discharged=FALSE"
    )
    return {
        "active_nurses":   nurses,
        "active_patients": patients,
        "discharged_patients": discharged,
        "occupied_rooms":  rooms,
    }


# ── Nurse approval helpers ────────────────────────────────────────────────────

async def get_nurses_by_status(
    conn: asyncpg.Connection, status: str | None = None
) -> list[asyncpg.Record]:
    """Return nurse users, optionally filtered by approval_status."""
    if status:
        return await conn.fetch(
            """
            SELECT id, username, full_name, ward, is_active, approval_status, created_at
            FROM users
            WHERE role = 'nurse' AND approval_status = $1
            ORDER BY created_at DESC
            """,
            status,
        )
    return await conn.fetch(
        """
        SELECT id, username, full_name, ward, is_active, approval_status, created_at
        FROM users
        WHERE role = 'nurse'
        ORDER BY approval_status, created_at DESC
        """
    )


async def set_nurse_approval(
    conn: asyncpg.Connection, user_id: int, status: str
) -> asyncpg.Record | None:
    """Set a nurse's approval_status. Returns the updated row or None if not found."""
    return await conn.fetchrow(
        """
        UPDATE users
        SET approval_status = $1
        WHERE id = $2 AND role = 'nurse'
        RETURNING id, username, full_name, ward, approval_status
        """,
        status, user_id,
    )


# ── Room management helpers ───────────────────────────────────────────────────

async def get_all_rooms(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    """
    Return all rooms with the current patient (if any) and attending nurse.
    Left-joins the active patient so unoccupied rooms still appear.
    """
    return await conn.fetch(
        """
        SELECT
            r.id, r.room_number, r.ward, r.status, r.notes, r.updated_at,
            p.full_name  AS patient_name,
            p.diagnosis  AS diagnosis,
            p.attending  AS attending_username,
            u.full_name  AS attending_name
        FROM rooms r
        LEFT JOIN patients p
               ON p.room_number = r.room_number AND p.is_discharged = FALSE
        LEFT JOIN users u
               ON u.username = p.attending AND u.role = 'nurse'
        ORDER BY r.room_number
        """
    )


async def create_room(
    conn: asyncpg.Connection,
    room_number: str,
    ward: str | None,
    status: str,
    notes: str | None,
) -> asyncpg.Record:
    """Create a new room. Raises asyncpg.UniqueViolationError if it already exists."""
    return await conn.fetchrow(
        """
        INSERT INTO rooms (room_number, ward, status, notes)
        VALUES ($1, $2, $3, $4)
        RETURNING id, room_number, ward, status, notes, updated_at
        """,
        room_number, ward, status, notes,
    )


async def update_room(
    conn: asyncpg.Connection,
    room_number: str,
    status: str | None = None,
    ward: str | None = None,
    notes: str | None = None,
) -> asyncpg.Record | None:
    """Update a room's status/ward/notes. Returns updated row or None if not found."""
    # Build a dynamic update but keep it simple and safe with fixed params.
    row = await conn.fetchrow("SELECT * FROM rooms WHERE room_number = $1", room_number)
    if row is None:
        return None
    new_status = status if status is not None else row["status"]
    new_ward   = ward   if ward   is not None else row["ward"]
    new_notes  = notes  if notes  is not None else row["notes"]
    return await conn.fetchrow(
        """
        UPDATE rooms
        SET status = $1, ward = $2, notes = $3, updated_at = NOW()
        WHERE room_number = $4
        RETURNING id, room_number, ward, status, notes, updated_at
        """,
        new_status, new_ward, new_notes, room_number,
    )


# ── Patient assignment helpers ────────────────────────────────────────────────

async def assign_patient_nurse(
    conn: asyncpg.Connection, patient_id: int, nurse_username: str | None
) -> asyncpg.Record | None:
    """Set the PRIMARY attending nurse for a patient (by patient id).

    None = unassign the primary. Also keeps the many-to-many patient_nurses
    table in sync: the primary is stored at priority_order 0 for that room.
    """
    row = await conn.fetchrow(
        """
        UPDATE patients
        SET attending = $1
        WHERE id = $2
        RETURNING id, full_name, room_number, attending
        """,
        nurse_username, patient_id,
    )
    if row is not None:
        room = row["room_number"]
        # Remove any existing priority-0 (primary) rows for this room.
        await conn.execute(
            "DELETE FROM patient_nurses WHERE room_number = $1 AND priority_order = 0",
            room,
        )
        if nurse_username:
            # Insert/upgrade the new primary at priority 0.
            await conn.execute(
                """
                INSERT INTO patient_nurses (room_number, nurse_username, priority_order)
                VALUES ($1, $2, 0)
                ON CONFLICT (room_number, nurse_username)
                DO UPDATE SET priority_order = 0
                """,
                room, nurse_username,
            )
    return row


async def add_patient_nurse(
    conn: asyncpg.Connection,
    room_number: str,
    nurse_username: str,
    priority_order: int | None = None,
) -> None:
    """Assign an ADDITIONAL (backup) nurse to a room's patient.

    If priority_order is None, append after the current lowest priority.
    """
    if priority_order is None:
        current_max = await conn.fetchval(
            "SELECT COALESCE(MAX(priority_order), -1) FROM patient_nurses WHERE room_number = $1",
            room_number,
        )
        priority_order = int(current_max) + 1
    await conn.execute(
        """
        INSERT INTO patient_nurses (room_number, nurse_username, priority_order)
        VALUES ($1, $2, $3)
        ON CONFLICT (room_number, nurse_username)
        DO UPDATE SET priority_order = EXCLUDED.priority_order
        """,
        room_number, nurse_username, priority_order,
    )


async def remove_patient_nurse(
    conn: asyncpg.Connection, room_number: str, nurse_username: str
) -> None:
    """Unassign a nurse from a room's patient (many-to-many)."""
    await conn.execute(
        "DELETE FROM patient_nurses WHERE room_number = $1 AND nurse_username = $2",
        room_number, nurse_username,
    )


async def get_nurses_for_room(conn: asyncpg.Connection, room_number: str) -> list[str]:
    """Return the room's assigned nurse usernames in priority order (primary first).

    Falls back to patients.attending if no patient_nurses rows exist yet, so the
    scheduler keeps working for rooms assigned the old single-nurse way.
    """
    rows = await conn.fetch(
        """
        SELECT nurse_username
        FROM patient_nurses
        WHERE room_number = $1
        ORDER BY priority_order ASC, nurse_username ASC
        """,
        room_number,
    )
    if rows:
        return [r["nurse_username"] for r in rows]
    # Back-compat fallback: the single attending nurse for an active patient.
    att = await conn.fetchval(
        """
        SELECT attending FROM patients
        WHERE room_number = $1 AND is_discharged = FALSE AND attending IS NOT NULL
        ORDER BY admitted_at DESC
        LIMIT 1
        """,
        room_number,
    )
    return [att] if att else []


async def _sync_patient_nurses(conn: asyncpg.Connection) -> None:
    """Backfill patient_nurses from attending, and seed the canonical
    multi-nurse scheduling scenario on first run (idempotent).

    Canonical scenario (so the live system matches the simulation):
      • Room 4B (patient_raj): nurse_anna (primary), nurse_ben (backup)
      • Room 4C (patient_sara): nurse_anna (primary), nurse_carol (backup)
    So nurse_anna covers BOTH 4B and 4C. If anna is busy with 4B, a 4C alert
    fails over to her backup on 4C (nurse_carol).
    """
    # 1. Backfill: every active patient with an attending gets a priority-0 row.
    await conn.execute(
        """
        INSERT INTO patient_nurses (room_number, nurse_username, priority_order)
        SELECT p.room_number, p.attending, 0
        FROM patients p
        WHERE p.is_discharged = FALSE AND p.attending IS NOT NULL
        ON CONFLICT (room_number, nurse_username) DO NOTHING
        """
    )

    # 2. Seed backup nurses for the canonical scenario — only if those rooms
    #    exist and the backup isn't already assigned. Safe to run every startup.
    scenario = [
        # (room, backup_nurse, priority)
        ("4B", "nurse_ben",   1),
        ("4C", "nurse_carol", 1),
    ]
    for room, backup, prio in scenario:
        room_exists = await conn.fetchval(
            "SELECT 1 FROM patients WHERE room_number = $1 AND is_discharged = FALSE LIMIT 1",
            room,
        )
        nurse_exists = await conn.fetchval(
            "SELECT 1 FROM users WHERE username = $1 AND role = 'nurse' LIMIT 1",
            backup,
        )
        if room_exists and nurse_exists:
            await conn.execute(
                """
                INSERT INTO patient_nurses (room_number, nurse_username, priority_order)
                VALUES ($1, $2, $3)
                ON CONFLICT (room_number, nurse_username) DO NOTHING
                """,
                room, backup, prio,
            )
    logger.info("patient_nurses synced (backfill + canonical scenario seed)")


async def set_patient_discharge(
    conn: asyncpg.Connection, patient_id: int, discharged: bool
) -> asyncpg.Record | None:
    """Mark a patient discharged / re-admitted. Returns updated row or None."""
    return await conn.fetchrow(
        """
        UPDATE patients
        SET is_discharged = $1
        WHERE id = $2
        RETURNING id, full_name, room_number, is_discharged
        """,
        discharged, patient_id,
    )


async def get_approved_nurse_usernames(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    """Return approved, active nurses with their routing attributes."""
    return await conn.fetch(
        """
        SELECT username, full_name, ward, competencies, status, is_supervisor
        FROM users
        WHERE role = 'nurse' AND is_active = TRUE AND approval_status = 'approved'
        ORDER BY full_name
        """
    )


# ── Routing attribute helpers ─────────────────────────────────────────────────

_VALID_STATUS = {"available", "in_patient_room", "on_break", "off_duty"}


async def set_nurse_status(conn: asyncpg.Connection, username: str, status: str) -> bool:
    """Set a nurse's live status. Returns False for an unknown status value."""
    if status not in _VALID_STATUS:
        return False
    res = await conn.execute(
        "UPDATE users SET status = $2 WHERE username = $1 AND role = 'nurse'",
        username, status,
    )
    return res.endswith("1")


async def set_nurse_competencies(conn: asyncpg.Connection, username: str, competencies: str) -> bool:
    """Set a nurse's competency list (comma-separated skill tokens)."""
    cleaned = ",".join(
        t.strip().lower().replace(" ", "_") for t in competencies.split(",") if t.strip()
    )
    res = await conn.execute(
        "UPDATE users SET competencies = $2 WHERE username = $1 AND role = 'nurse'",
        username, cleaned,
    )
    return res.endswith("1")


async def set_nurse_supervisor(conn: asyncpg.Connection, username: str, is_supervisor: bool) -> bool:
    res = await conn.execute(
        "UPDATE users SET is_supervisor = $2 WHERE username = $1 AND role = 'nurse'",
        username, is_supervisor,
    )
    return res.endswith("1")


async def set_patient_acuity(conn: asyncpg.Connection, room_number: str, acuity: int | None) -> None:
    """Set the ESI acuity (1–5) for the active patient in a room."""
    await conn.execute(
        """
        UPDATE patients SET acuity = $2
        WHERE room_number = $1 AND is_discharged = FALSE
        """,
        room_number, acuity,
    )


async def get_nurse_routing_map(conn: asyncpg.Connection) -> dict[str, dict]:
    """username -> {full_name, ward, competencies:set, status, is_supervisor}
    for every approved, active nurse. Used to build scheduler NurseStates."""
    rows = await conn.fetch(
        """
        SELECT username, full_name, ward, competencies, status, is_supervisor
        FROM users
        WHERE role = 'nurse' AND is_active = TRUE AND approval_status = 'approved'
        """
    )
    out: dict[str, dict] = {}
    for r in rows:
        comps = {c for c in (r["competencies"] or "").split(",") if c}
        out[r["username"]] = {
            "full_name": r["full_name"],
            "ward": r["ward"],
            "competencies": comps,
            "status": r["status"] or "available",
            "is_supervisor": bool(r["is_supervisor"]),
        }
    return out


async def get_supervisor_usernames(conn: asyncpg.Connection, ward: str | None = None) -> list[str]:
    """Supervisors (same ward first if ward given), for escalation."""
    rows = await conn.fetch(
        """
        SELECT username, ward FROM users
        WHERE role = 'nurse' AND is_active = TRUE AND approval_status = 'approved'
          AND is_supervisor = TRUE
        ORDER BY full_name
        """
    )
    if ward:
        same = [r["username"] for r in rows if r["ward"] == ward]
        other = [r["username"] for r in rows if r["ward"] != ward]
        return same + other
    return [r["username"] for r in rows]


async def get_patient_acuity_by_room(conn: asyncpg.Connection, room_number: str) -> int | None:
    return await conn.fetchval(
        """
        SELECT acuity FROM patients
        WHERE room_number = $1 AND is_discharged = FALSE
        ORDER BY admitted_at DESC LIMIT 1
        """,
        room_number,
    )


async def get_ward_for_room(conn: asyncpg.Connection, room_number: str) -> str | None:
    return await conn.fetchval(
        "SELECT ward FROM rooms WHERE room_number = $1", room_number
    )


# ── Analytics helpers ─────────────────────────────────────────────────────────

async def get_room_status_summary(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    """Count of rooms grouped by status — for the occupancy donut chart."""
    return await conn.fetch(
        "SELECT status, COUNT(*) AS count FROM rooms GROUP BY status ORDER BY count DESC"
    )


# ── Voice-note helpers ────────────────────────────────────────────────────────

async def create_voice_note(
    conn:           asyncpg.Connection,
    *,
    room_id:        str,
    alert_id:       int | None,
    sender_user_id: int | None,
    sender_role:    str,
    sender_name:    str,
    filename:       str,
    mime_type:      str,
    duration_ms:    int,
    language:       str,
    original_text:  str | None,
) -> int:
    """Insert a voice-note metadata row and return its new id."""
    return int(await conn.fetchval(
        """
        INSERT INTO voice_notes
            (room_id, alert_id, sender_user_id, sender_role, sender_name,
             filename, mime_type, duration_ms, language, original_text, created_at)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10, NOW())
        RETURNING id
        """,
        room_id, alert_id, sender_user_id, sender_role, sender_name,
        filename, mime_type, duration_ms, language, original_text,
    ))


async def get_voice_notes(
    conn: asyncpg.Connection, room_id: str, limit: int = 20
) -> list[asyncpg.Record]:
    """Return the most recent voice notes for a room, newest first."""
    return await conn.fetch(
        """
        SELECT id, room_id, alert_id, sender_role, sender_name, created_at,
               duration_ms, language, filename, mime_type, original_text
        FROM voice_notes
        WHERE room_id = $1
        ORDER BY created_at DESC
        LIMIT $2
        """,
        room_id, limit,
    )


async def get_voice_note(
    conn: asyncpg.Connection, note_id: int
) -> asyncpg.Record | None:
    """Return a single voice-note row by id, or None."""
    return await conn.fetchrow("SELECT * FROM voice_notes WHERE id = $1", note_id)
