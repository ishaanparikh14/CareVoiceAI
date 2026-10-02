"""
test_alerts_router_manual.py — exercises the actual FastAPI router
(routers/alerts.py) over HTTP via TestClient, against a real temp-file
SQLite DB. Does not require PostgreSQL since it only mounts alerts_router.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tmpdir = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(tmpdir, "router_test.db")
os.environ["URGENT_ESCALATION_TIMEOUT_SECONDS"] = "300"

import asyncio  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import database  # noqa: E402
from routers import alerts as alerts_router  # noqa: E402

app = FastAPI()
app.include_router(alerts_router.router)

_failures = []


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        _failures.append(name)


asyncio.run(database.init_db())

client = TestClient(app)

# ── POST /alerts/manual (Critical, no timer) ──────────────────────────────────
resp = client.post("/alerts/manual", params={"room_id": "6F", "patient_name": "Jane"})
check("manual alert -> 201", resp.status_code == 201)
manual_alert_id = resp.json()["alert_id"]

resp = client.get(f"/alerts/{manual_alert_id}")
body = resp.json()
check("manual alert priority is Critical", body["priority"] == "Critical")
check("manual alert has no escalation_deadline", body["escalation_deadline"] is None)
check("manual alert escalated is False", body["escalated"] is False)

# ── Seed an Urgent alert directly via insert_alert (simulating pipeline) ─────
async def seed_urgent():
    import aiosqlite
    from config import settings
    async with aiosqlite.connect(settings.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        aid = await database.insert_alert(
            db, room_id="4B", priority="Urgent", intent="Pain",
            distress_score=0.0, transcript="my leg hurts badly",
        )
        await db.commit()
    return aid

urgent_id = asyncio.run(seed_urgent())

resp = client.get(f"/alerts/{urgent_id}")
body = resp.json()
check("urgent alert has escalation_deadline", body["escalation_deadline"] is not None)
check("urgent alert initial_priority == priority == Urgent", body["initial_priority"] == "Urgent" == body["priority"])

# ── ACK does not attend, does not stop escalation eligibility ────────────────
resp = client.post(f"/alerts/{urgent_id}/ack", json={"ack_by": "Nurse-A"})
check("ack -> 200", resp.status_code == 200)
resp = client.get(f"/alerts/{urgent_id}")
body = resp.json()
check("after ack: acknowledged True", body["acknowledged"] is True)
check("after ack: attended still False", body["attended"] is False)

# Duplicate ACK -> 409
resp = client.post(f"/alerts/{urgent_id}/ack", json={"ack_by": "Nurse-B"})
check("duplicate ack -> 409", resp.status_code == 409)

# ── ATTEND is separate from ACK ───────────────────────────────────────────────
resp = client.post(f"/alerts/{urgent_id}/attend", json={"attended_by": "Nurse-A"})
check("attend -> 200", resp.status_code == 200)
data = resp.json()
check("attend response attended True", data["attended"] is True)
check("attend response attended_by correct", data["attended_by"] == "Nurse-A")

resp = client.get(f"/alerts/{urgent_id}")
body = resp.json()
check("after attend: attended True", body["attended"] is True)
check("after attend: attended_by correct", body["attended_by"] == "Nurse-A")
check("after attend: priority still Urgent (attend != escalate)", body["priority"] == "Urgent")

# Duplicate ATTEND -> 409
resp = client.post(f"/alerts/{urgent_id}/attend", json={"attended_by": "Nurse-C"})
check("duplicate attend -> 409", resp.status_code == 409)

# Attend a non-existent alert -> 404
resp = client.post("/alerts/999999/attend", json={"attended_by": "Nurse-A"})
check("attend nonexistent -> 404", resp.status_code == 404)

# ── GET /alerts/latest includes the new fields for every row ─────────────────
resp = client.get("/alerts/latest?limit=10")
check("latest -> 200", resp.status_code == 200)
alerts = resp.json()["alerts"]
check("latest alerts include escalation fields", all("escalation_deadline" in a and "attended" in a for a in alerts))

print()
if _failures:
    print(f"{len(_failures)} FAILURE(S)")
    sys.exit(1)
print("ALL CHECKS PASSED")
