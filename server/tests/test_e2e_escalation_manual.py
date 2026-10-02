"""
test_e2e_escalation_manual.py — end-to-end: alert creation (via insert_alert,
simulating the pipeline), the escalation background pass, and the WS
broadcast payload shape that Android will parse.
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["URGENT_ESCALATION_TIMEOUT_SECONDS"] = "1"
os.environ["ESCALATION_CHECK_INTERVAL_SECONDS"] = "1"
tmpdir = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(tmpdir, "e2e.db")

import aiosqlite  # noqa: E402
from config import settings  # noqa: E402
import database  # noqa: E402
import escalation_manager  # noqa: E402

_failures = []
_broadcasts = []


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        _failures.append(name)


async def fake_broadcast(message: str):
    _broadcasts.append(json.loads(message))


async def main():
    await database.init_db()
    escalation_manager.set_broadcaster(fake_broadcast)

    async with aiosqlite.connect(settings.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        aid = await database.insert_alert(
            db, room_id="4B", priority="Urgent", intent="Pain",
            distress_score=0.0, transcript="severe abdominal pain and dizziness",
        )
        await db.commit()

    check("no broadcast before deadline passes", len(_broadcasts) == 0)

    await asyncio.sleep(1.5)
    escalated = await escalation_manager.run_once()
    check("escalation fired for our alert", aid in escalated)
    check("exactly one broadcast sent", len(_broadcasts) == 1)

    payload = _broadcasts[0]
    check("payload event == new_alert", payload["event"] == "new_alert")
    check("payload alert_id matches", payload["alert_id"] == aid)
    check("payload priority == Critical", payload["priority"] == "Critical")
    check("payload initial_priority == Urgent", payload["initial_priority"] == "Urgent")
    check("payload escalated == True", payload["escalated"] is True)
    check("payload attended == False", payload["attended"] is False)
    check("payload room_id preserved", payload["room_id"] == "4B")
    check("payload has intent", payload["intent"] == "Pain")
    check("payload has transcript", "dizziness" in payload["transcript"])

    print()
    print("Sample escalation WS payload (what Android receives):")
    print(json.dumps(payload, indent=2))

    if _failures:
        print(f"\n{len(_failures)} FAILURE(S)")
        sys.exit(1)
    print("\nALL CHECKS PASSED")


asyncio.run(main())
