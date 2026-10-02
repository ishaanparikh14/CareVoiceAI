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
    _pool = await asyncpg.create_pool(
        dsn      = settings.DATABASE_URL,
        min_size = 2,
        max_size = 10,
    )
    logger.info("PostgreSQL pool opened → %s:%d/%s",
                settings.PG_HOST, settings.PG_PORT, settings.PG_DATABASE)


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

-- Index for fast login lookup
CREATE INDEX IF NOT EXISTS idx_users_username ON users (username);
-- Index for room-number lookups from patient app
CREATE INDEX IF NOT EXISTS idx_patients_room  ON patients (room_number);
-- Index for joining patient → user
CREATE INDEX IF NOT EXISTS idx_patients_user  ON patients (user_id);
-- Index for room lookups
CREATE INDEX IF NOT EXISTS idx_rooms_number    ON rooms (room_number);

CREATE TABLE IF NOT EXISTS alerts (
    id                  BIGSERIAL PRIMARY KEY,
    room_id             TEXT NOT NULL,
    patient_name        TEXT NOT NULL DEFAULT 'Patient',
    language            TEXT NOT NULL DEFAULT 'en',
    priority            TEXT NOT NULL CHECK (priority IN ('Critical','Urgent','Routine')),
    initial_priority    TEXT NOT NULL CHECK (initial_priority IN ('Critical','Urgent','Routine')),
    intent              TEXT NOT NULL,
    distress_score      DOUBLE PRECISION NOT NULL DEFAULT 0,
    transcript          TEXT NOT NULL,
    wav_path            TEXT,
    nlp_summary         TEXT NOT NULL DEFAULT '',
    acknowledged        BOOLEAN NOT NULL DEFAULT FALSE,
    ack_by              TEXT,
    ack_at              TIMESTAMPTZ,
    attended            BOOLEAN NOT NULL DEFAULT FALSE,
    attended_by         TEXT,
    attended_at         TIMESTAMPTZ,
    escalation_deadline TIMESTAMPTZ,
    escalated_at        TIMESTAMPTZ,
    escalation_count    INTEGER NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_room ON alerts (room_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_pending ON alerts (acknowledged, attended, priority);

CREATE TABLE IF NOT EXISTS voice_notes (
    id              BIGSERIAL PRIMARY KEY,
    room_id         TEXT NOT NULL,
    alert_id        BIGINT REFERENCES alerts(id) ON DELETE SET NULL,
    sender_user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    sender_role     TEXT NOT NULL CHECK (sender_role IN ('nurse','patient')),
    sender_name     TEXT NOT NULL,
    filename        TEXT NOT NULL,
    mime_type       TEXT NOT NULL DEFAULT 'audio/wav',
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    language        TEXT NOT NULL DEFAULT 'en',
    original_text   TEXT,
    translated_text TEXT,
    source_language TEXT,
    target_language TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_voice_notes_room ON voice_notes (room_id, created_at DESC);
"""


_EMR_DDL = """
CREATE TABLE IF NOT EXISTS patient_conditions (
    id SERIAL PRIMARY KEY, patient_id INT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    condition_name TEXT NOT NULL, status TEXT DEFAULT 'active', severity TEXT,
    notes TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS patient_allergies (
    id SERIAL PRIMARY KEY, patient_id INT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    allergen TEXT NOT NULL, reaction TEXT, severity TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS patient_medications (
    id SERIAL PRIMARY KEY, patient_id INT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    medication_name TEXT NOT NULL, dose TEXT, route TEXT, frequency TEXT,
    status TEXT DEFAULT 'active', instructions TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS patient_diet_orders (
    id SERIAL PRIMARY KEY, patient_id INT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    diet_type TEXT NOT NULL, restrictions TEXT, status TEXT DEFAULT 'active',
    instructions TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS patient_restrictions (
    id SERIAL PRIMARY KEY, patient_id INT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    restriction_type TEXT NOT NULL, restriction_value TEXT NOT NULL,
    severity TEXT, instructions TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS doctor_instructions (
    id SERIAL PRIMARY KEY, patient_id INT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    instruction TEXT NOT NULL, priority TEXT DEFAULT 'normal', active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS nursing_notes (
    id SERIAL PRIMARY KEY, patient_id INT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    note TEXT NOT NULL, author TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS patient_vitals (
    id SERIAL PRIMARY KEY, patient_id INT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    measured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), heart_rate NUMERIC,
    systolic_bp NUMERIC, diastolic_bp NUMERIC, spo2 NUMERIC,
    temperature_c NUMERIC, blood_glucose NUMERIC
);
CREATE TABLE IF NOT EXISTS hospital_food_options (
    id SERIAL PRIMARY KEY, name TEXT NOT NULL UNIQUE, diet_tags TEXT[] NOT NULL DEFAULT '{}',
    allergens TEXT[] NOT NULL DEFAULT '{}', available BOOLEAN NOT NULL DEFAULT TRUE,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_emr_conditions_patient ON patient_conditions(patient_id);
CREATE INDEX IF NOT EXISTS idx_emr_allergies_patient ON patient_allergies(patient_id);
CREATE INDEX IF NOT EXISTS idx_emr_meds_patient ON patient_medications(patient_id);
CREATE INDEX IF NOT EXISTS idx_emr_diet_patient ON patient_diet_orders(patient_id);
CREATE INDEX IF NOT EXISTS idx_emr_restrictions_patient ON patient_restrictions(patient_id);
CREATE INDEX IF NOT EXISTS idx_emr_doctor_patient ON doctor_instructions(patient_id);
CREATE INDEX IF NOT EXISTS idx_emr_notes_patient ON nursing_notes(patient_id);
CREATE INDEX IF NOT EXISTS idx_emr_vitals_patient ON patient_vitals(patient_id);
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


async def _migrate_alert_voice_schema(conn: asyncpg.Connection) -> None:
    """Add columns needed by the current alert/voice-note implementation."""
    await conn.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS patient_name TEXT NOT NULL DEFAULT 'Patient'")
    await conn.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS language TEXT NOT NULL DEFAULT 'en'")
    await conn.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS nlp_summary TEXT NOT NULL DEFAULT ''")
    await conn.execute("ALTER TABLE voice_notes ADD COLUMN IF NOT EXISTS duration_ms INTEGER NOT NULL DEFAULT 0")
    await conn.execute("ALTER TABLE voice_notes ADD COLUMN IF NOT EXISTS language TEXT NOT NULL DEFAULT 'en'")
    await conn.execute("ALTER TABLE voice_notes ADD COLUMN IF NOT EXISTS original_text TEXT")
    await conn.execute("ALTER TABLE voice_notes ADD COLUMN IF NOT EXISTS translated_text TEXT")
    await conn.execute("ALTER TABLE voice_notes ADD COLUMN IF NOT EXISTS source_language TEXT")
    await conn.execute("ALTER TABLE voice_notes ADD COLUMN IF NOT EXISTS target_language TEXT")
    # preferred_language stores the nurse's UI-selected translation target language.
    # Defaults to 'en' so existing rows are backward-compatible.
    await conn.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS preferred_language TEXT NOT NULL DEFAULT 'en'"
    )


async def init_pg_db() -> None:
    """
    Create tables (idempotent) and seed sample data on first run.
    Called once from main.py lifespan startup.
    """
    assert _pool is not None
    async with _pool.acquire() as conn:
        # Run DDL
        await conn.execute(_DDL)
        await conn.execute(_EMR_DDL)
        await _migrate_alert_voice_schema(conn)
        logger.info("PostgreSQL + EMR schema ensured")

        # Migrate: drop old role CHECK constraint and replace with one that
        # includes 'admin'.  Safe to run repeatedly — does nothing if the
        # constraint already allows 'admin'.
        await _migrate_role_constraint(conn)

        # Migrate: add approval_status column to older databases.
        await _migrate_approval_status(conn)

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

        # Seed the demo EMR only after patient seed/migration has completed.
        await _seed_emr_demo_data(conn)

        # Sync the rooms table with any room_numbers referenced by patients.
        # This runs every startup so newly-admitted rooms always appear.
        await _sync_rooms(conn)


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


async def _seed_emr_demo_data(conn: asyncpg.Connection) -> None:
    """Seed safe demo EMR context only when a patient has no EMR rows yet."""
    patients = await conn.fetch(
        """
        SELECT p.id AS id, u.username
        FROM patients p
        JOIN users u ON u.id = p.user_id
        WHERE p.is_discharged = FALSE
        """
    )
    for p in patients:
        pid = p["id"]
        exists = await conn.fetchval("SELECT 1 FROM patient_conditions WHERE patient_id=$1 LIMIT 1", pid)
        if exists:
            continue
        if p["username"] == "patient_raj":
            await conn.execute("INSERT INTO patient_conditions(patient_id,condition_name,severity,notes) VALUES($1,'Type 2 Diabetes','moderate','Controlled inpatient diabetes')", pid)
            await conn.execute("INSERT INTO patient_diet_orders(patient_id,diet_type,restrictions,instructions) VALUES($1,'Diabetic diet','Avoid concentrated sugars','Follow hospital diabetic meal plan')", pid)
            await conn.execute("INSERT INTO patient_restrictions(patient_id,restriction_type,restriction_value,instructions) VALUES($1,'mobility','assist with ambulation','Post-operative fall precautions')", pid)
            await conn.execute("INSERT INTO doctor_instructions(patient_id,instruction,priority) VALUES($1,'Report severe abdominal pain immediately','high')", pid)
            await conn.execute("INSERT INTO nursing_notes(patient_id,note,author) VALUES($1,'Post-operative monitoring ongoing.','nurse_anna')", pid)
            await conn.execute("INSERT INTO patient_vitals(patient_id,heart_rate,systolic_bp,diastolic_bp,spo2,temperature_c,blood_glucose) VALUES($1,96,138,88,97,37.2,128)", pid)
        else:
            await conn.execute("INSERT INTO patient_diet_orders(patient_id,diet_type,restrictions,instructions) VALUES($1,'Regular diet','','Follow current hospital meal plan')", pid)

    food = [
        ("Diabetic meal", ["diabetic"], []),
        ("Vegetable soup", ["regular", "diabetic"], []),
        ("Sugar-free yogurt", ["regular", "diabetic"], []),
        ("Regular meal", ["regular"], []),
    ]
    for name, tags, allergens in food:
        await conn.execute("""INSERT INTO hospital_food_options(name,diet_tags,allergens) VALUES($1,$2,$3)
                              ON CONFLICT(name) DO NOTHING""", name, tags, allergens)


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


async def update_user_profile(
    conn:               asyncpg.Connection,
    user_id:            int,
    full_name:          str | None = None,
    ward:               str | None = None,
    preferred_language: str | None = None,
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
    if preferred_language is not None:
        # Validate to avoid storing garbage.
        lang = preferred_language.strip().lower()[:2]
        if lang not in {"en", "hi"}:
            lang = "en"
        await conn.execute(
            "UPDATE users SET preferred_language = $1 WHERE id = $2",
            lang, user_id,
        )


async def get_nurse_preferred_language_for_room(
    conn:    asyncpg.Connection,
    room_id: str,
) -> str:
    """
    Look up the preferred_language of the nurse attending the patient in room_id.

    Two-stage lookup (most-specific first):

    Stage 1 — canonical patients.attending → users JOIN:
        Works when the patient record's ``attending`` field exactly matches
        the nurse's username in the ``users`` table.

    Stage 2 — most-recent nurse voice-note sender for this room:
        Fallback for cases where ``patients.attending`` is NULL, blank, or
        does not match the nurse's login username (e.g. was set to full name
        instead of username, or was never populated).  Uses the sender_user_id
        stored on the voice_notes row so the join is always exact.

    Every failure path is logged explicitly; this function NEVER silently
    converts an unresolvable lookup into English without a log entry.
    """

    # ── Stage 1: patients.attending → users ──────────────────────────────────
    row = await conn.fetchrow(
        """
        SELECT u.preferred_language, u.username
        FROM   patients p
        JOIN   users    u ON u.username = p.attending
        WHERE  p.room_number  = $1
          AND  p.is_discharged = FALSE
          AND  u.role          = 'nurse'
          AND  u.is_active     = TRUE
        LIMIT 1
        """,
        room_id,
    )
    if row:
        raw  = (row["preferred_language"] or "").strip().lower()
        lang = raw[:2] if raw else ""
        if lang in {"en", "hi"}:
            logger.info(
                "NURSE_LANG_LOOKUP stage=attending room=%s nurse=%s lang=%s",
                room_id, row["username"], lang,
            )
            return lang
        # Row found but preferred_language is empty / invalid — nurse never
        # called PATCH /auth/me.  Log and fall through to Stage 2.
        logger.warning(
            "NURSE_LANG_LOOKUP stage=attending room=%s nurse=%s "
            "preferred_language=%r is empty or invalid — Stage 2 fallback",
            room_id, row["username"], row["preferred_language"],
        )
    else:
        # Diagnose WHY Stage 1 found nothing so operators can fix the data.
        diag = await conn.fetchrow(
            """
            SELECT p.room_number, p.attending, p.is_discharged
            FROM   patients p
            WHERE  p.room_number = $1
            LIMIT 1
            """,
            room_id,
        )
        if diag is None:
            logger.warning(
                "NURSE_LANG_LOOKUP stage=attending room=%s → no patient row "
                "in patients table — Stage 2 fallback",
                room_id,
            )
        elif diag["is_discharged"]:
            logger.warning(
                "NURSE_LANG_LOOKUP stage=attending room=%s → patient is "
                "discharged — Stage 2 fallback",
                room_id,
            )
        elif not diag["attending"]:
            logger.warning(
                "NURSE_LANG_LOOKUP stage=attending room=%s → patients.attending "
                "is NULL/empty — Stage 2 fallback",
                room_id,
            )
        else:
            logger.warning(
                "NURSE_LANG_LOOKUP stage=attending room=%s → attending=%r does "
                "not match any active nurse username — Stage 2 fallback",
                room_id, diag["attending"],
            )

    # ── Stage 2: most-recent nurse voice-note sender for this room ────────────
    row2 = await conn.fetchrow(
        """
        SELECT u.preferred_language, u.username
        FROM   voice_notes vn
        JOIN   users       u  ON u.id = vn.sender_user_id
        WHERE  vn.room_id     = $1
          AND  vn.sender_role = 'nurse'
          AND  u.is_active    = TRUE
        ORDER BY vn.created_at DESC
        LIMIT 1
        """,
        room_id,
    )
    if row2:
        raw  = (row2["preferred_language"] or "").strip().lower()
        lang = raw[:2] if raw else ""
        if lang in {"en", "hi"}:
            logger.info(
                "NURSE_LANG_LOOKUP stage=voice_note_history room=%s nurse=%s lang=%s",
                room_id, row2["username"], lang,
            )
            return lang
        logger.warning(
            "NURSE_LANG_LOOKUP stage=voice_note_history room=%s nurse=%s "
            "preferred_language=%r is empty/invalid — explicit 'en' fallback",
            room_id, row2["username"], row2["preferred_language"],
        )
    else:
        logger.warning(
            "NURSE_LANG_LOOKUP stage=voice_note_history room=%s → no nurse "
            "voice notes found for this room — explicit 'en' fallback",
            room_id,
        )

    # ── Explicit final fallback ───────────────────────────────────────────────
    # Both stages failed.  We cannot determine the nurse's language, so we
    # return 'en' — but we LOG THIS CLEARLY so operators can see the problem.
    # When source_language is also 'en', the translation will be skipped
    # (same-language check in the router).  Operators MUST fix either
    # patients.attending or ensure nurses have sent at least one voice note.
    logger.error(
        "NURSE_LANG_LOOKUP room=%s → BOTH STAGES FAILED.  "
        "Cannot determine nurse language.  "
        "Fix: set patients.attending=<nurse username> OR ensure nurse has sent "
        "a prior voice note to this room.  "
        "Defaulting to 'en' — en→en translation WILL BE SKIPPED.",
        room_id,
    )
    return "en"


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
    """Set the attending nurse for a patient (by patient id). None = unassign."""
    return await conn.fetchrow(
        """
        UPDATE patients
        SET attending = $1
        WHERE id = $2
        RETURNING id, full_name, room_number, attending
        """,
        nurse_username, patient_id,
    )


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
    """Return approved, active nurses for populating assignment dropdowns."""
    return await conn.fetch(
        """
        SELECT username, full_name, ward
        FROM users
        WHERE role = 'nurse' AND is_active = TRUE AND approval_status = 'approved'
        ORDER BY full_name
        """
    )


# ── Analytics helpers ─────────────────────────────────────────────────────────

async def get_room_status_summary(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    """Count of rooms grouped by status — for the occupancy donut chart."""
    return await conn.fetch(
        "SELECT status, COUNT(*) AS count FROM rooms GROUP BY status ORDER BY count DESC"
    )
