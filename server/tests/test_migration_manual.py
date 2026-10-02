"""
test_migration_manual.py — verifies database.init_db() safely migrates a
pre-existing "legacy" alerts table (the ORIGINAL schema, no escalation
columns) that already has data in it, without destroying anything.
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tmpdir = tempfile.mkdtemp()
db_path = os.path.join(tmpdir, "legacy.db")
os.environ["DB_PATH"] = db_path

import aiosqlite  # noqa: E402

LEGACY_DDL = """
CREATE TABLE alerts (
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
"""

_failures = []


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        _failures.append(name)


async def main():
    # 1. Create a legacy DB with one pre-existing row, as if from the old app version.
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(LEGACY_DDL)
        await db.execute(
            "INSERT INTO alerts (room_id, priority, intent, distress_score, transcript, created_at) "
            "VALUES ('1A', 'Urgent', 'Pain', 0.6, 'old row before migration', '2026-01-01T00:00:00.000Z')"
        )
        await db.commit()

    # 2. Now import our modules (config picks up DB_PATH from env) and run init_db().
    import database  # noqa: E402
    await database.init_db()

    # 3. Verify the pre-existing row survived intact and got backfilled correctly.
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM alerts") as cur:
            rows = await cur.fetchall()

    check("migration: legacy row still present", len(rows) == 1)
    row = dict(rows[0])
    check("migration: legacy transcript preserved", row["transcript"] == "old row before migration")
    check("migration: legacy priority preserved", row["priority"] == "Urgent")
    check("migration: initial_priority backfilled from priority", row["initial_priority"] == "Urgent")
    check("migration: attended defaults to 0", row["attended"] == 0)
    check("migration: escalation_count defaults to 0", row["escalation_count"] == 0)
    check("migration: escalation_deadline is NULL for pre-existing row (not retroactively computed)",
          row["escalation_deadline"] is None)

    # 4. New inserts after migration should work normally end-to-end.
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        new_id = await database.insert_alert(
            db, room_id="1A", priority="Urgent", intent="Pain",
            distress_score=0.0, transcript="new row after migration",
        )
        await db.commit()
        new_row = await database.get_alert_by_id(db, new_id)
    check("post-migration insert works and sets escalation_deadline", new_row["escalation_deadline"] is not None)

    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S)")
        sys.exit(1)
    print("ALL CHECKS PASSED")


asyncio.run(main())
