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
    # Managed Postgres typically requires TLS. Since sslmode is stripped from the
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
    role            TEXT  NOT NULL CHECK (role IN ('nurse','patient','admin','canteen','pharmacy','floor_manager','doctor','dietician')),
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
    language       TEXT NOT NULL DEFAULT 'en',  -- 'en' | 'hi' | 'de'
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

# Department-staff accounts (per-person, NOT shared). floor managers handle
# housekeeping/facility requests for the floors they cover and may have a deputy.
# (username, plain_pw, full_name, role, department, floors_csv, deputy_username)
_SEED_DEPARTMENT_STAFF = [
    ("canteen_cathy",  "dept1234", "Cathy Lewis (Canteen)",       "canteen",       "canteen",      "",    None),
    ("pharmacy_pete",  "dept1234", "Pete Rao (Pharmacy)",         "pharmacy",      "pharmacy",     "",    None),
    ("floor_mgr_4",    "dept1234", "Fatima Noor (Floor 4 Mgr)",   "floor_manager", "housekeeping", "4",   "floor_mgr_5"),
    ("floor_mgr_5",    "dept1234", "Sam Patel (Floor 5 Mgr)",     "floor_manager", "housekeeping", "5",   "floor_mgr_4"),
    ("floor_mgr_6",    "dept1234", "Liu Wei (Floor 6 Mgr)",       "floor_manager", "housekeeping", "6",   None),
    # v3 clinical support roles (log in through the Staff tab).
    ("doctor_dev",     "doc12345", "Dr. Dev Sharma",              "doctor",        "doctor",       "",    None),
    ("dietician_demo", "diet1234", "Meera Iyer (Dietician)",      "dietician",     "dietician",    "",    None),
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

        # Migrate: department-routing columns (users.department + floor-manager
        # coverage, rooms.floor, patients.diet_restriction) and seed demo data.
        await _migrate_department_columns(conn)

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
        # Recreate the constraint whenever any required role is missing (this
        # already added 'admin'; now it also adds the department-staff roles).
        required_roles = ("admin", "canteen", "pharmacy", "floor_manager", "doctor", "dietician")
        if defn and any(r not in defn for r in required_roles):
            await conn.execute(f'ALTER TABLE users DROP CONSTRAINT "{conname}"')
            await conn.execute(
                "ALTER TABLE users ADD CONSTRAINT users_role_check "
                "CHECK (role IN ('nurse','patient','admin','canteen','pharmacy','floor_manager','doctor','dietician'))"
            )
            logger.info("Migrated users.role CHECK constraint to include admin + department roles")


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


async def _migrate_department_columns(conn: asyncpg.Connection) -> None:
    """Add department-routing columns (idempotent) and seed demo data.

    users.department   — for department-staff accounts: 'canteen'|'pharmacy'|'housekeeping'.
                         Preferred over inventing a role per department so adding
                         e.g. a dietitian later is data, not a migration.
    users.floors       — comma-separated floors a floor_manager covers (housekeeping).
    users.deputy_username — a floor_manager's deputy (covers breaks/shifts).
    rooms.floor        — the room's floor (NOT parsed from room_number).
    patients.diet_restriction — 'none'|'npo'|'diabetic'|'dysphagia'|'low_sodium'|...
                         Editable in the patient profile; gates Food/Water routing.
    """
    await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS department TEXT")
    await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS floors TEXT NOT NULL DEFAULT ''")
    await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS deputy_username TEXT")
    await conn.execute("ALTER TABLE rooms ADD COLUMN IF NOT EXISTS floor TEXT")
    await conn.execute("ALTER TABLE patients ADD COLUMN IF NOT EXISTS diet_restriction TEXT NOT NULL DEFAULT 'none'")

    # Seed room floors for the demo rooms (explicit, not string-parsed).
    room_floors = [("4B", "4"), ("4C", "4"), ("5A", "5"), ("5B", "5"), ("6A", "6")]
    for room, fl in room_floors:
        await conn.execute(
            "UPDATE rooms SET floor = $2 WHERE room_number = $1 AND (floor IS NULL OR floor = '')",
            room, fl,
        )

    # Seed RANDOM-but-deterministic diet restrictions across the demo patients so
    # both the diet gate (NURSE) and the open path (CANTEEN) are exercisable.
    # Deterministic here = fixed demo assignment; still editable in the profile.
    demo_diets = [
        ("patient_raj",  "veg_only"),        # vegetarian menu recommendations
        ("patient_sara", "none"),            # no restriction — full menu
        ("patient_tom",  "gluten_free"),     # gluten-free recommendations
        ("patient_lima", "peanut_allergy"),  # peanut-safe recommendations
        ("patient_zoe",  "jain"),            # Jain recommendations
    ]
    for username, diet in demo_diets:
        await conn.execute(
            """
            UPDATE patients p
            SET diet_restriction = $2
            FROM users u
            WHERE p.user_id = u.id AND u.username = $1 AND p.diet_restriction = 'none'
            """,
            username, diet,
        )

    # Seed per-person department-staff accounts (NOT shared logins).
    await _seed_department_staff(conn)

    # Menu + medication catalogues for the order-lifecycle feature.
    await _migrate_menu_and_meds(conn)

    # v3: dietician plans, attender PINs, demo EMR (HIS/EMR adapter data).
    await _migrate_v3(conn)
    logger.info("Department columns ensured + demo data seeded (floors, diets, dept staff, menu, meds)")


async def _migrate_v3(conn: asyncpg.Connection) -> None:
    """v3 tables (idempotent): dietician plans, attender PINs, and the local demo
    EMR used by the HIS/EMR adapter (FHIR R4-style shapes)."""
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dietician_plans (
            room_number   TEXT PRIMARY KEY,
            allowed_items TEXT NOT NULL DEFAULT '',
            avoid_items   TEXT NOT NULL DEFAULT '',
            notes         TEXT NOT NULL DEFAULT '',
            food_cleared  BOOLEAN NOT NULL DEFAULT FALSE,
            updated_by    TEXT,
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    # Attender PIN per room (hashed). Set by the nurse or admin; the attender
    # enters it on the patient's device to unlock food ordering.
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS attender_access (
            room_number  TEXT PRIMARY KEY,
            pin_hash     TEXT NOT NULL,
            attender_name TEXT NOT NULL DEFAULT 'Attender',
            failed       INT  NOT NULL DEFAULT 0,
            locked_until TIMESTAMPTZ,
            updated_by   TEXT,
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    # ── Demo EMR (LocalDemoEMR). Real hospitals plug in the FHIR adapter. ──
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS emr_observations (
            id           SERIAL PRIMARY KEY,
            room_number  TEXT NOT NULL,
            category     TEXT NOT NULL,          -- vital-signs | laboratory
            code         TEXT NOT NULL,          -- e.g. heart_rate, hba1c
            display      TEXT NOT NULL,
            value        TEXT NOT NULL,
            unit         TEXT NOT NULL DEFAULT '',
            ref_range    TEXT NOT NULL DEFAULT '',
            flag         TEXT NOT NULL DEFAULT 'normal',  -- normal | high | low
            effective_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_emr_obs_room ON emr_observations (room_number, effective_at DESC)")
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS emr_conditions (
            id          SERIAL PRIMARY KEY,
            room_number TEXT NOT NULL,
            display     TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'active',
            onset       TEXT NOT NULL DEFAULT ''
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS emr_encounters (
            id          SERIAL PRIMARY KEY,
            room_number TEXT NOT NULL,
            kind        TEXT NOT NULL,           -- admission | procedure | note | visit
            summary     TEXT NOT NULL,
            author      TEXT NOT NULL DEFAULT '',
            occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_emr_enc_room ON emr_encounters (room_number, occurred_at DESC)")
    # Write-back store (DocumentReference / Communication) for summaries + alert logs.
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS emr_documents (
            id          SERIAL PRIMARY KEY,
            room_number TEXT NOT NULL,
            doc_type    TEXT NOT NULL,           -- patient_summary | shift_summary | alert_log
            title       TEXT NOT NULL,
            content     TEXT NOT NULL,
            author      TEXT NOT NULL DEFAULT '',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    await _seed_demo_emr(conn)
    await _seed_attender_pins(conn)


# Demo clinical data per seeded room (illustrative only, not real patients).
_DEMO_EMR = {
    "4B": {
        "conditions": [("Laparoscopic cholecystectomy (post-op day 2)", "active", "2026-10-22"),
                       ("Type 2 diabetes mellitus", "active", "2019"),
                       ("Hypertension", "active", "2017")],
        "vitals": [("heart_rate", "Heart rate", "84", "bpm", "60-100", "normal"),
                   ("bp", "Blood pressure", "138/86", "mmHg", "<130/80", "high"),
                   ("temp", "Temperature", "37.4", "°C", "36.1-37.5", "normal"),
                   ("spo2", "Oxygen saturation", "97", "%", "95-100", "normal"),
                   ("resp_rate", "Respiratory rate", "16", "/min", "12-20", "normal")],
        "labs": [("glucose", "Blood glucose (fasting)", "156", "mg/dL", "70-100", "high"),
                 ("hba1c", "HbA1c", "7.8", "%", "<5.7", "high"),
                 ("hb", "Haemoglobin", "12.9", "g/dL", "13.5-17.5", "low"),
                 ("wbc", "White cell count", "9.1", "x10^9/L", "4.0-11.0", "normal")],
        "encounters": [("admission", "Admitted for elective laparoscopic cholecystectomy.", "Dr. Dev Sharma"),
                       ("procedure", "Laparoscopic cholecystectomy completed without complication.", "Dr. Dev Sharma"),
                       ("note", "Pain controlled on oral analgesia; mobilising with assistance.", "Nurse Anna")],
    },
    "4C": {
        "conditions": [("Closed fracture of right tibial shaft", "active", "2026-10-20"),
                       ("Mild asthma", "active", "2012")],
        "vitals": [("heart_rate", "Heart rate", "76", "bpm", "60-100", "normal"),
                   ("bp", "Blood pressure", "118/74", "mmHg", "<130/80", "normal"),
                   ("temp", "Temperature", "36.9", "°C", "36.1-37.5", "normal"),
                   ("spo2", "Oxygen saturation", "99", "%", "95-100", "normal"),
                   ("pain_score", "Pain score", "4", "/10", "0-3", "high")],
        "labs": [("hb", "Haemoglobin", "13.1", "g/dL", "12.0-15.5", "normal"),
                 ("crp", "C-reactive protein", "18", "mg/L", "<5", "high"),
                 ("vitd", "Vitamin D", "22", "ng/mL", "30-100", "low")],
        "encounters": [("admission", "Admitted after fall; X-ray confirmed tibial shaft fracture.", "Dr. Dev Sharma"),
                       ("procedure", "Intramedullary nailing of right tibia.", "Dr. Dev Sharma"),
                       ("note", "Physiotherapy started; partial weight bearing with crutches.", "Physio")],
    },
    "5A": {
        "conditions": [("Hypertensive urgency", "active", "2026-10-21"),
                       ("Chronic kidney disease stage 3a", "active", "2021")],
        "vitals": [("heart_rate", "Heart rate", "92", "bpm", "60-100", "normal"),
                   ("bp", "Blood pressure", "162/98", "mmHg", "<130/80", "high"),
                   ("temp", "Temperature", "36.8", "°C", "36.1-37.5", "normal"),
                   ("spo2", "Oxygen saturation", "96", "%", "95-100", "normal")],
        "labs": [("creatinine", "Creatinine", "1.6", "mg/dL", "0.7-1.3", "high"),
                 ("egfr", "eGFR", "48", "mL/min", ">60", "low"),
                 ("potassium", "Potassium", "4.9", "mmol/L", "3.5-5.0", "normal")],
        "encounters": [("admission", "Admitted with severe headache and BP 190/110.", "Dr. Dev Sharma"),
                       ("note", "IV labetalol given; BP trending down.", "Nurse Ben")],
    },
    "5B": {
        "conditions": [("Acute appendicitis, post appendectomy", "active", "2026-10-23")],
        "vitals": [("heart_rate", "Heart rate", "88", "bpm", "60-100", "normal"),
                   ("bp", "Blood pressure", "122/78", "mmHg", "<130/80", "normal"),
                   ("temp", "Temperature", "37.8", "°C", "36.1-37.5", "high"),
                   ("spo2", "Oxygen saturation", "98", "%", "95-100", "normal")],
        "labs": [("wbc", "White cell count", "12.4", "x10^9/L", "4.0-11.0", "high"),
                 ("crp", "C-reactive protein", "34", "mg/L", "<5", "high")],
        "encounters": [("admission", "Admitted with right iliac fossa pain; ultrasound positive.", "Dr. Dev Sharma"),
                       ("procedure", "Laparoscopic appendectomy.", "Dr. Dev Sharma")],
    },
    "6A": {
        "conditions": [("Acute asthma exacerbation", "active", "2026-10-22"),
                       ("Allergic rhinitis", "active", "2015")],
        "vitals": [("heart_rate", "Heart rate", "98", "bpm", "60-100", "normal"),
                   ("bp", "Blood pressure", "116/72", "mmHg", "<130/80", "normal"),
                   ("spo2", "Oxygen saturation", "94", "%", "95-100", "low"),
                   ("resp_rate", "Respiratory rate", "22", "/min", "12-20", "high"),
                   ("peak_flow", "Peak flow", "310", "L/min", ">400", "low")],
        "labs": [("eos", "Eosinophils", "0.6", "x10^9/L", "0.0-0.5", "high")],
        "encounters": [("admission", "Admitted with wheeze and breathlessness.", "Dr. Dev Sharma"),
                       ("note", "Nebulised salbutamol 4-hourly; improving.", "Nurse Carol")],
    },
}


async def _seed_demo_emr(conn: asyncpg.Connection) -> None:
    """Seed the demo EMR once per room (skips rooms that already have data)."""
    for room, d in _DEMO_EMR.items():
        exists = await conn.fetchval("SELECT 1 FROM emr_observations WHERE room_number = $1 LIMIT 1", room)
        if exists:
            continue
        for disp, status, onset in d["conditions"]:
            await conn.execute(
                "INSERT INTO emr_conditions (room_number, display, status, onset) VALUES ($1,$2,$3,$4)",
                room, disp, status, onset)
        for i, (code, disp, val, unit, rng, flag) in enumerate(d["vitals"]):
            await conn.execute(
                "INSERT INTO emr_observations (room_number, category, code, display, value, unit, ref_range, flag, "
                "effective_at) VALUES ($1,'vital-signs',$2,$3,$4,$5,$6,$7, NOW() - ($8 || ' minutes')::interval)",
                room, code, disp, val, unit, rng, flag, str(30 + i))
        for i, (code, disp, val, unit, rng, flag) in enumerate(d["labs"]):
            await conn.execute(
                "INSERT INTO emr_observations (room_number, category, code, display, value, unit, ref_range, flag, "
                "effective_at) VALUES ($1,'laboratory',$2,$3,$4,$5,$6,$7, NOW() - ($8 || ' hours')::interval)",
                room, code, disp, val, unit, rng, flag, str(6 + i))
        for i, (kind, summary, author) in enumerate(d["encounters"]):
            await conn.execute(
                "INSERT INTO emr_encounters (room_number, kind, summary, author, occurred_at) "
                "VALUES ($1,$2,$3,$4, NOW() - ($5 || ' hours')::interval)",
                room, kind, summary, author, str((len(d["encounters"]) - i) * 20))
    logger.info("Demo EMR ensured for %d rooms", len(_DEMO_EMR))


async def _seed_attender_pins(conn: asyncpg.Connection) -> None:
    """Demo attender PIN 1234 for each seeded room (only where none is set)."""
    from passlib.context import CryptContext
    ctx = CryptContext(schemes=["argon2"], deprecated="auto")
    for room in _DEMO_EMR:
        has = await conn.fetchval("SELECT 1 FROM attender_access WHERE room_number = $1", room)
        if not has:
            await conn.execute(
                "INSERT INTO attender_access (room_number, pin_hash, attender_name, updated_by) "
                "VALUES ($1,$2,'Attender','seed')",
                room, ctx.hash("1234"))


# ── v3 query helpers ──────────────────────────────────────────────────────────

async def get_dietician_plan(room_number: str) -> dict | None:
    if _pool is None or not room_number:
        return None
    async with _pool.acquire() as conn:
        r = await conn.fetchrow("SELECT * FROM dietician_plans WHERE room_number = $1", room_number.strip())
    return dict(r) if r else None


async def set_dietician_plan(room_number: str, *, allowed_items: str, avoid_items: str,
                             notes: str, food_cleared: bool, by: str) -> dict:
    async with _pool.acquire() as conn:
        r = await conn.fetchrow(
            """
            INSERT INTO dietician_plans (room_number, allowed_items, avoid_items, notes, food_cleared,
                                         updated_by, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,NOW())
            ON CONFLICT (room_number) DO UPDATE SET allowed_items=EXCLUDED.allowed_items,
                avoid_items=EXCLUDED.avoid_items, notes=EXCLUDED.notes,
                food_cleared=EXCLUDED.food_cleared, updated_by=EXCLUDED.updated_by, updated_at=NOW()
            RETURNING *
            """,
            room_number.strip(), allowed_items, avoid_items, notes, food_cleared, by)
    return dict(r)


async def get_menu_raw() -> list[dict]:
    if _pool is None:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, name, description, tags FROM menu_items WHERE is_active = TRUE ORDER BY id")
    return [dict(r) for r in rows]


async def set_attender_pin(room_number: str, pin: str, name: str, by: str) -> None:
    from passlib.context import CryptContext
    ctx = CryptContext(schemes=["argon2"], deprecated="auto")
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO attender_access (room_number, pin_hash, attender_name, failed, locked_until, updated_by, updated_at)
            VALUES ($1,$2,$3,0,NULL,$4,NOW())
            ON CONFLICT (room_number) DO UPDATE SET pin_hash=EXCLUDED.pin_hash,
                attender_name=EXCLUDED.attender_name, failed=0, locked_until=NULL,
                updated_by=EXCLUDED.updated_by, updated_at=NOW()
            """,
            room_number.strip(), ctx.hash(pin), name, by)


async def verify_attender_pin(room_number: str, pin: str) -> tuple[bool, str]:
    """Return (ok, reason). Locks the room for 15 minutes after 5 bad PINs."""
    from passlib.context import CryptContext
    ctx = CryptContext(schemes=["argon2"], deprecated="auto")
    async with _pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT pin_hash, failed, locked_until, locked_until > NOW() AS locked, attender_name "
            "FROM attender_access WHERE room_number = $1", room_number.strip())
        if r is None:
            return False, "no_pin"
        if r["locked"]:
            return False, "locked"
        if ctx.verify(pin, r["pin_hash"]):
            await conn.execute("UPDATE attender_access SET failed = 0, locked_until = NULL WHERE room_number = $1",
                               room_number.strip())
            return True, r["attender_name"]
        failed = (r["failed"] or 0) + 1
        if failed >= 5:
            await conn.execute(
                "UPDATE attender_access SET failed = 0, locked_until = NOW() + INTERVAL '15 minutes' "
                "WHERE room_number = $1", room_number.strip())
            return False, "locked"
        await conn.execute("UPDATE attender_access SET failed = $2 WHERE room_number = $1",
                           room_number.strip(), failed)
        return False, "bad_pin"


async def _migrate_menu_and_meds(conn: asyncpg.Connection) -> None:
    """Create + seed the canteen menu and per-patient medication catalogues.

    menu_items: a fixed 10-item canteen menu. `tags` is a comma-separated set of
      dietary tags the item SATISFIES (e.g. 'veg_only,jain,gluten_free'); the
      canteen UI recommends items whose tags cover the patient's restriction.
    patient_medications: each patient's meds, split by kind:
      'prescribed' (clinical — needs nurse approval) vs 'general' (OTC — may go
      straight to pharmacy).
    """
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS menu_items (
            id          SERIAL PRIMARY KEY,
            name        TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            tags        TEXT NOT NULL DEFAULT '',   -- comma-sep dietary tags satisfied
            is_active   BOOLEAN NOT NULL DEFAULT TRUE
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS patient_medications (
            id           SERIAL PRIMARY KEY,
            room_number  TEXT NOT NULL,
            name         TEXT NOT NULL,
            kind         TEXT NOT NULL CHECK (kind IN ('prescribed','general')),
            dosage       TEXT NOT NULL DEFAULT '',
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (room_number, name)
        )
        """
    )
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_patient_meds_room ON patient_medications (room_number)")

    # Seed the 10-item menu once (idempotent on name).
    # Dietary tags vocabulary: veg_only, jain, gluten_free, peanut_allergy_safe,
    # green_veg, none(implicitly satisfied by all).
    menu = [
        ("Steamed Rice & Dal",        "Plain dal with steamed rice",              "veg_only,jain,gluten_free,peanut_allergy_safe"),
        ("Grilled Chicken & Veg",     "Grilled chicken with steamed vegetables",  "gluten_free,peanut_allergy_safe,green_veg"),
        ("Palak Paneer",              "Spinach and cottage cheese curry",         "veg_only,gluten_free,green_veg"),
        ("Jain Veg Thali",            "No onion/garlic/root vegetables",          "veg_only,jain,peanut_allergy_safe"),
        ("Gluten-Free Khichdi",       "Rice-lentil porridge, mildly spiced",      "veg_only,jain,gluten_free,peanut_allergy_safe"),
        ("Mixed Green Salad",         "Lettuce, spinach, cucumber, olive oil",    "veg_only,jain,gluten_free,peanut_allergy_safe,green_veg"),
        ("Peanut-Free Veg Pulao",     "Vegetable rice, cooked without peanuts",   "veg_only,gluten_free,peanut_allergy_safe,green_veg"),
        ("Fish Curry & Rice",         "Light fish curry with rice",               "gluten_free,peanut_allergy_safe"),
        ("Vegetable Soup",            "Clear broth with seasonal vegetables",     "veg_only,jain,gluten_free,peanut_allergy_safe,green_veg"),
        ("Fruit Bowl",                "Seasonal fresh fruit",                     "veg_only,jain,gluten_free,peanut_allergy_safe"),
    ]
    for name, desc, tags in menu:
        await conn.execute(
            """
            INSERT INTO menu_items (name, description, tags)
            VALUES ($1,$2,$3)
            ON CONFLICT DO NOTHING
            """,
            name, desc, tags,
        )
    # menu_items has no unique on name by default; guard against dup seeding.
    await conn.execute(
        """
        DELETE FROM menu_items a USING menu_items b
        WHERE a.id > b.id AND a.name = b.name
        """
    )

    # Seed demo medications per room (prescribed = clinical; general = OTC).
    # general meds a patient may request straight from pharmacy without a nurse.
    meds = [
        # room, name, kind, dosage
        ("4B", "Metformin",     "prescribed", "500mg twice daily"),
        ("4B", "Paracetamol",   "general",    "500mg as needed"),
        ("4B", "Antacid",       "general",    "as needed"),
        ("4C", "Amoxicillin",   "prescribed", "250mg three times daily"),
        ("4C", "Paracetamol",   "general",    "500mg as needed"),
        ("4C", "Cough Syrup",   "general",    "10ml as needed"),
        ("5A", "Amlodipine",    "prescribed", "5mg once daily"),
        ("5A", "Paracetamol",   "general",    "500mg as needed"),
        ("5B", "Pantoprazole",  "prescribed", "40mg once daily"),
        ("5B", "ORS",           "general",    "as needed"),
        ("6A", "Salbutamol",    "prescribed", "inhaler, 2 puffs"),
        ("6A", "Paracetamol",   "general",    "500mg as needed"),
        ("6A", "Lozenges",      "general",    "as needed"),
    ]
    for room, name, kind, dosage in meds:
        await conn.execute(
            """
            INSERT INTO patient_medications (room_number, name, kind, dosage)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (room_number, name) DO NOTHING
            """,
            room, name, kind, dosage,
        )


async def _seed_department_staff(conn: asyncpg.Connection) -> None:
    """Insert per-person canteen / pharmacy / floor-manager accounts (idempotent).

    Floor managers cover specific floors (housekeeping) and may name a deputy.
    """
    from passlib.context import CryptContext
    pwd_ctx = CryptContext(schemes=["argon2"], deprecated="auto")

    for username, plain_pw, full_name, role, department, floors, deputy in _SEED_DEPARTMENT_STAFF:
        hashed = pwd_ctx.hash(plain_pw)
        await conn.execute(
            """
            INSERT INTO users (username, password_hash, full_name, role, department, floors, deputy_username, approval_status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'approved')
            ON CONFLICT (username) DO UPDATE
                SET department = EXCLUDED.department,
                    floors = EXCLUDED.floors,
                    deputy_username = EXCLUDED.deputy_username
            """,
            username, hashed, full_name, role, department, floors, deputy,
        )
    logger.info("Department staff accounts ensured (%d)", len(_SEED_DEPARTMENT_STAFF))


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
    conn:             asyncpg.Connection,
    user_id:          int,
    full_name:        str | None = None,
    room_number:      str | None = None,
    age:              int | None = None,
    diagnosis:        str | None = None,
    attending:        str | None = None,
    diet_restriction: str | None = None,
) -> None:
    """Update mutable patient-specific fields. Also syncs full_name on patients row."""
    if full_name is not None:
        await conn.execute(
            "UPDATE patients SET full_name = $1 WHERE user_id = $2",
            full_name, user_id,
        )
    if diet_restriction is not None:
        await conn.execute(
            "UPDATE patients SET diet_restriction = $1 WHERE user_id = $2",
            diet_restriction, user_id,
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
            p.attending, p.admitted_at, p.is_discharged, p.diet_restriction,
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


# ── Department-routing query helpers ──────────────────────────────────────────

async def get_floor_for_room(room_number: str) -> str | None:
    """Resolve a room's floor from the rooms table (own pooled connection)."""
    if _pool is None or not room_number:
        return None
    try:
        async with _pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT floor FROM rooms WHERE room_number = $1", room_number.strip()
            )
    except Exception:
        return None


async def get_diet_restriction_for_room(room_number: str) -> str | None:
    """Resolve the active patient's diet restriction for a room (gates catering)."""
    if _pool is None or not room_number:
        return None
    try:
        async with _pool.acquire() as conn:
            return await conn.fetchval(
                """
                SELECT diet_restriction FROM patients
                WHERE room_number = $1 AND is_discharged = FALSE
                ORDER BY admitted_at DESC LIMIT 1
                """,
                room_number.strip(),
            )
    except Exception:
        return None


async def get_department_user(conn: asyncpg.Connection, department: str) -> asyncpg.Record | None:
    """Return a single active account for a global department (canteen/pharmacy)."""
    return await conn.fetchrow(
        """
        SELECT username, full_name, department, floors, deputy_username
        FROM users
        WHERE department = $1 AND is_active = TRUE
        ORDER BY username
        LIMIT 1
        """,
        department,
    )


async def get_floor_manager_chain(conn: asyncpg.Connection, floor: str | None) -> list[dict]:
    """Return the housekeeping escalation chain for a floor, in order:
       [primary floor manager, deputy, ...any other housekeeping staff].
    Each item: {username, full_name, is_deputy}. Empty if none.
    """
    if not floor:
        # No floor → fall back to all housekeeping staff.
        rows = await conn.fetch(
            """
            SELECT username, full_name FROM users
            WHERE department = 'housekeeping' AND is_active = TRUE
            ORDER BY username
            """
        )
        return [{"username": r["username"], "full_name": r["full_name"], "is_deputy": False} for r in rows]

    rows = await conn.fetch(
        """
        SELECT username, full_name, floors, deputy_username
        FROM users
        WHERE department = 'housekeeping' AND is_active = TRUE
        ORDER BY username
        """
    )
    chain: list[dict] = []
    deputies: list[str] = []
    covered = []
    for r in rows:
        floors = {f.strip() for f in (r["floors"] or "").split(",") if f.strip()}
        if floor in floors:
            covered.append(r)
            if r["deputy_username"]:
                deputies.append(r["deputy_username"])
    for r in covered:
        chain.append({"username": r["username"], "full_name": r["full_name"], "is_deputy": False})
    # Append named deputies (if not already in the chain).
    seen = {c["username"] for c in chain}
    for dep in deputies:
        if dep in seen:
            continue
        drow = await conn.fetchrow(
            "SELECT username, full_name FROM users WHERE username = $1 AND is_active = TRUE", dep
        )
        if drow:
            chain.append({"username": drow["username"], "full_name": drow["full_name"], "is_deputy": True})
            seen.add(dep)
    # Finally, any other housekeeping staff as a last-resort pool.
    for r in rows:
        if r["username"] not in seen:
            chain.append({"username": r["username"], "full_name": r["full_name"], "is_deputy": True})
            seen.add(r["username"])
    return chain


# ── Menu + medication query helpers (order-lifecycle feature) ─────────────────

# Maps a patient diet_restriction to the menu tag an item must carry to be safe.
# 'none' means every active item is allowed.
_DIET_TO_REQUIRED_TAG = {
    "none":            None,
    "veg_only":        "veg_only",
    "jain":            "jain",
    "gluten_free":     "gluten_free",
    "peanut_allergy":  "peanut_allergy_safe",
    "needs_green_veg": "green_veg",
    "green_veg":       "green_veg",
    # Legacy clinical diets still route via nurse; if a menu is ever requested
    # for them we fall back to the safest universally-tagged items.
    "npo":             "gluten_free",
    "dysphagia":       "gluten_free",
    "diabetic":        "gluten_free",
    "low_sodium":      "gluten_free",
}


async def get_menu_for_diet(diet_restriction: str | None) -> list[dict]:
    """Return active menu items, flagged as recommended/allowed for a diet.

    Every active item is returned; `recommended=True` means it satisfies the
    patient's restriction (so the canteen UI can highlight safe choices while
    still showing the full menu)."""
    if _pool is None:
        return []
    diet = (diet_restriction or "none").strip().lower()
    required = _DIET_TO_REQUIRED_TAG.get(diet, None)
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name, description, tags FROM menu_items WHERE is_active = TRUE ORDER BY id"
        )
    out = []
    for r in rows:
        tags = {t for t in (r["tags"] or "").split(",") if t}
        recommended = (required is None) or (required in tags)
        out.append({
            "id": r["id"], "name": r["name"], "description": r["description"],
            "tags": sorted(tags), "recommended": recommended,
        })
    return out


async def get_patient_medications(room_number: str) -> list[dict]:
    """Return a patient's medications (prescribed + general) for a room."""
    if _pool is None or not room_number:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, kind, dosage FROM patient_medications
            WHERE room_number = $1 ORDER BY kind, name
            """,
            room_number.strip(),
        )
    return [{"id": r["id"], "name": r["name"], "kind": r["kind"], "dosage": r["dosage"]} for r in rows]


async def match_general_medication(room_number: str, transcript: str) -> dict | None:
    """If the transcript clearly names one of this patient's GENERAL meds, return
    it (so the request can go straight to pharmacy). Prescribed meds never match
    here — they must go through nurse approval. Returns None if no general-med
    name is found in the transcript."""
    if not room_number or not transcript:
        return None
    meds = await get_patient_medications(room_number)
    t = transcript.lower()
    for m in meds:
        if m["kind"] == "general" and m["name"].lower() in t:
            return m
    return None


async def nurse_covers_room(conn: asyncpg.Connection, nurse_username: str, room_number: str) -> bool:
    """True if this nurse is mapped to the room (attending OR in patient_nurses).
    Authorization gate for nurse diet/medication edits."""
    if not room_number:
        return False
    nurses = await get_nurses_for_room(conn, room_number)
    return nurse_username in nurses


async def set_patient_diet_by_room(conn: asyncpg.Connection, room_number: str, diet: str) -> bool:
    """Set the active patient's diet_restriction for a room. Returns True if a row changed."""
    res = await conn.execute(
        "UPDATE patients SET diet_restriction = $2 WHERE room_number = $1 AND is_discharged = FALSE",
        room_number, diet,
    )
    return res.endswith(tuple(str(n) for n in range(1, 10))) or res.split()[-1] != "0"


async def add_patient_medication(
    conn: asyncpg.Connection, room_number: str, name: str, kind: str, dosage: str
) -> None:
    await conn.execute(
        """
        INSERT INTO patient_medications (room_number, name, kind, dosage)
        VALUES ($1,$2,$3,$4)
        ON CONFLICT (room_number, name) DO UPDATE SET kind = EXCLUDED.kind, dosage = EXCLUDED.dosage
        """,
        room_number, name, kind, dosage,
    )


async def remove_patient_medication(conn: asyncpg.Connection, room_number: str, name: str) -> None:
    await conn.execute(
        "DELETE FROM patient_medications WHERE room_number = $1 AND name = $2",
        room_number, name,
    )


async def get_menu_item(item_id: int) -> dict | None:
    if _pool is None:
        return None
    async with _pool.acquire() as conn:
        r = await conn.fetchrow("SELECT id, name, description, tags FROM menu_items WHERE id = $1", item_id)
    return dict(r) if r else None
