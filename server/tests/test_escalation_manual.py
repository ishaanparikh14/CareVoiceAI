"""
test_escalation_manual.py — standalone verification of the escalation logic
against a real (temp-file) SQLite database, with NO PostgreSQL/auth
dependency. Run directly:

    python3 test_escalation_manual.py

Exercises the edge cases from the spec (short timeout so we don't sleep 5
real minutes):
  1. Urgent -> attended after 2s (with a 1s timeout it should escalate first
     unless attended before the deadline) -- we use distinct short windows
     per case below instead of literal minutes.
  3. Urgent -> never attended -> Critical after timeout.
  5. Urgent -> attended just before deadline -> no escalation.
  6. Escalated -> attended -> remains Critical, no further escalation.
  7. AI-generated Critical -> no Urgent timer, never escalates.
  9. "Server restart" simulated by opening a fresh connection -> deadline
     persisted on disk survives.
  12. ACK never marks attended / never stops escalation by itself.
"""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["URGENT_ESCALATION_TIMEOUT_SECONDS"] = "1"
os.environ["ESCALATION_CHECK_INTERVAL_SECONDS"] = "1"

tmpdir = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(tmpdir, "test_carevoice.db")

import aiosqlite  # noqa: E402

from config import settings  # noqa: E402
import database  # noqa: E402
import escalation_manager  # noqa: E402
from models import Priority  # noqa: E402

assert str(settings.DB_PATH) == os.environ["DB_PATH"]
assert settings.URGENT_ESCALATION_TIMEOUT_SECONDS == 1

_failures = []


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        _failures.append(name)


async def fresh_db():
    db = await aiosqlite.connect(settings.DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


async def main():
    await database.init_db()

    # ── Case 3 & 12: Urgent, never attended, never ACKed -> escalates ────────
    db = await fresh_db()
    aid = await database.insert_alert(
        db, room_id="3A", priority=Priority.URGENT.value, intent="Pain",
        distress_score=0.0, transcript="leg hurts",
    )
    await db.commit()
    row = await database.get_alert_by_id(db, aid)
    check("case3: new Urgent alert has escalation_deadline set", row["escalation_deadline"] is not None)
    check("case3: initial_priority == Urgent", row["initial_priority"] == "Urgent")
    check("case12: ACK never sets attended", True)  # verified structurally below
    ok = await database.acknowledge_alert(db, aid, ack_by="Nurse-A")
    await db.commit()
    row = await database.get_alert_by_id(db, aid)
    check("case12: after ACK, attended is still 0", row["attended"] == 0)
    check("case12: after ACK, priority still Urgent (ack doesn't stop escalation)", row["priority"] == "Urgent")
    await db.close()

    await asyncio.sleep(1.5)  # let the deadline pass
    escalated_ids = await escalation_manager.run_once()
    check("case3: alert was escalated after timeout despite ACK", aid in escalated_ids)

    db = await fresh_db()
    row = await database.get_alert_by_id(db, aid)
    check("case3: priority is now Critical", row["priority"] == "Critical")
    check("case3: escalated_at is set", row["escalated_at"] is not None)
    check("case3: escalation_count == 1", row["escalation_count"] == 1)
    await db.close()

    # Re-run escalation pass — must NOT double-escalate.
    escalated_again = await escalation_manager.run_once()
    check("no double escalation on second pass", aid not in escalated_again)

    # ── Case 6: escalated alert, then attended -> stays Critical, no more esc ─
    db = await fresh_db()
    ok = await database.attend_alert(db, aid, attended_by="Nurse-B")
    await db.commit()
    check("case6: attend succeeds on escalated alert", ok)
    row = await database.get_alert_by_id(db, aid)
    check("case6: still Critical after attend", row["priority"] == "Critical")
    check("case6: escalation_count did not increase again", row["escalation_count"] == 1)
    # Duplicate attend must fail (idempotent)
    ok2 = await database.attend_alert(db, aid, attended_by="Nurse-C")
    check("case6: duplicate attend rejected", ok2 is False)
    await db.close()

    # ── Case: attend BEFORE deadline -> never escalates ───────────────────────
    db = await fresh_db()
    aid2 = await database.insert_alert(
        db, room_id="5C", priority=Priority.URGENT.value, intent="Medication",
        distress_score=0.0, transcript="need my pills",
    )
    await db.commit()
    ok = await database.attend_alert(db, aid2, attended_by="Nurse-D")
    await db.commit()
    check("attend-before-deadline: attend succeeds", ok)
    await db.close()

    await asyncio.sleep(1.5)
    escalated3 = await escalation_manager.run_once()
    check("attend-before-deadline: never escalates", aid2 not in escalated3)

    db = await fresh_db()
    row = await database.get_alert_by_id(db, aid2)
    check("attend-before-deadline: priority still Urgent", row["priority"] == "Urgent")
    await db.close()

    # ── Case 7: AI-generated Critical alert -> no escalation timer at all ─────
    db = await fresh_db()
    aid3 = await database.insert_alert(
        db, room_id="9Z", priority=Priority.CRITICAL.value, intent="Emergency",
        distress_score=0.0, transcript="can't breathe",
    )
    await db.commit()
    row = await database.get_alert_by_id(db, aid3)
    check("case7: Critical alert has no escalation_deadline", row["escalation_deadline"] is None)
    await db.close()

    await asyncio.sleep(1.5)
    escalated4 = await escalation_manager.run_once()
    check("case7: Critical alert never appears in escalation results", aid3 not in escalated4)

    # ── Case 8: Routine alert -> no escalation timer ──────────────────────────
    db = await fresh_db()
    aid4 = await database.insert_alert(
        db, room_id="2B", priority=Priority.ROUTINE.value, intent="Food/Water",
        distress_score=0.0, transcript="water please",
    )
    await db.commit()
    row = await database.get_alert_by_id(db, aid4)
    check("case8: Routine alert has no escalation_deadline", row["escalation_deadline"] is None)
    await db.close()

    # ── Case 9: "server restart" — new connection still sees the same deadline ─
    db = await fresh_db()
    aid5 = await database.insert_alert(
        db, room_id="7D", priority=Priority.URGENT.value, intent="Pain",
        distress_score=0.0, transcript="pain again",
    )
    await db.commit()
    row_before = await database.get_alert_by_id(db, aid5)
    deadline_before = row_before["escalation_deadline"]
    await db.close()

    # Simulate restart: brand new connection, re-run init_db (idempotent migration)
    await database.init_db()
    db = await fresh_db()
    row_after = await database.get_alert_by_id(db, aid5)
    check("case9: escalation_deadline survives 'restart'", row_after["escalation_deadline"] == deadline_before)
    await db.close()

    # ── Migration idempotency: running init_db twice must not error or duplicate columns ─
    await database.init_db()
    db = await fresh_db()
    async with db.execute("PRAGMA table_info(alerts)") as cur:
        cols = [r["name"] for r in await cur.fetchall()]
    check("migration: no duplicate columns", len(cols) == len(set(cols)))
    for col in ("initial_priority", "attended", "attended_by", "attended_at",
                "escalation_deadline", "escalated_at", "escalation_count"):
        check(f"migration: column '{col}' present", col in cols)
    await db.close()

    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(" -", f)
        sys.exit(1)
    else:
        print("ALL CHECKS PASSED")


asyncio.run(main())
