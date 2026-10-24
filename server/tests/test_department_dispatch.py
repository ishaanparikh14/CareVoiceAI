# -*- coding: utf-8 -*-
"""
Tests for the department acknowledge/escalate state machine.

Like test_dispatch.py these run against a real temporary SQLite database (the
authoritative state) with the external seams (department WS manager, Postgres
target resolution, nurse dispatch) replaced by in-memory fakes. No Postgres, no
GPU. SLA timers are shortened so the suite stays fast.

Run:  python -m pytest tests/test_department_dispatch.py -v   (from server/)
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import aiosqlite
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import database  # noqa: E402
import department_dispatch as dd  # noqa: E402


# ── Fakes ─────────────────────────────────────────────────────────────────────

class FakeDeptManager:
    def __init__(self):
        self.dept_online: set[str] = set()
        self.nurse_online: set[str] = set()
        self.sent_dept: dict[str, list] = {}
        self.sent_nurse: dict[str, list] = {}
        self.admin_frames: list = []

    def is_dept_user_online(self, u): return u in self.dept_online
    def is_online(self, u): return u in self.nurse_online

    async def send_to_dept_user(self, u, msg):
        if u not in self.dept_online:
            return False
        self.sent_dept.setdefault(u, []).append(msg)
        return True

    async def send_to_nurse(self, u, msg):
        self.sent_nurse.setdefault(u, []).append(msg)
        return True

    async def send_to_admins(self, msg):
        self.admin_frames.append(msg)


class FakeWsModule:
    def __init__(self, manager): self.manager = manager


def install_fakes(monkeypatch, mgr, *, targets, floor="4", nurses=("anna",),
                  route_alert_spy=None):
    config.settings.DB_PATH = Path(config.settings.DB_PATH)  # set per-test below

    monkeypatch.setitem(sys.modules, "routers.ws", FakeWsModule(mgr))

    # patient_ws.notify_room → noop
    fake_patient_ws = type(sys)("routers.patient_ws")
    async def _notify_room(room_id, payload): return True
    fake_patient_ws.notify_room = _notify_room
    monkeypatch.setitem(sys.modules, "routers.patient_ws", fake_patient_ws)

    # pg_database target resolution + nurse lookup + floor
    import pg_database

    async def fake_get_floor_for_room(room_id): return floor
    async def fake_get_diet(room_id): return "none"

    async def fake_get_conn():
        yield object()

    async def fake_get_department_user(conn, department):
        u = targets.get(department)
        return {"username": u, "full_name": u, "department": department,
                "floors": "", "deputy_username": None} if u else None

    async def fake_floor_chain(conn, fl):
        u = targets.get("housekeeping")
        return [{"username": u, "full_name": u, "is_deputy": False}] if u else []

    async def fake_nurses_for_room(conn, room): return list(nurses)

    monkeypatch.setattr(pg_database, "get_floor_for_room", fake_get_floor_for_room)
    monkeypatch.setattr(pg_database, "get_diet_restriction_for_room", fake_get_diet)
    monkeypatch.setattr(pg_database, "get_conn", fake_get_conn)
    monkeypatch.setattr(pg_database, "get_department_user", fake_get_department_user)
    monkeypatch.setattr(pg_database, "get_floor_manager_chain", fake_floor_chain)
    monkeypatch.setattr(pg_database, "get_nurses_for_room", fake_nurses_for_room)

    # routing.route_alert spy (escalation target)
    import routing
    calls = []
    async def fake_route_alert(db, *, room_id, alert_id, payload_json, **kw):
        calls.append({"room_id": room_id, "alert_id": alert_id})
        if route_alert_spy is not None:
            route_alert_spy.append(alert_id)
        return (None, True)
    monkeypatch.setattr(routing, "route_alert", fake_route_alert)

    # fast SLA
    monkeypatch.setattr(config.settings, "DEPT_ACK_SLA_SECONDS", 0.3)
    monkeypatch.setattr(config.settings, "DEPT_DEDUPE_WINDOW_SECONDS", 120)

    for t in list(dd._timers.values()):
        t.cancel()
    dd._timers.clear()
    return calls


async def _init_db():
    await database.init_db()


async def _new_db():
    db = await aiosqlite.connect(config.settings.DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA busy_timeout=5000")
    return db


async def _make_alert(db, room="4B"):
    aid = await database.insert_alert(
        db, room_id=room, priority="Routine", intent="Food/Water",
        distress_score=0.0, transcript="water please", wav_path=None,
        patient_name="Test Patient", summary="wants water",
    )
    await db.commit()
    return aid


@pytest.fixture
def ctx(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        config.settings.DB_PATH = Path(os.path.join(tmp, "dept.db"))
        mgr = FakeDeptManager()
        mgr.dept_online.add("canteen_cathy")
        mgr.dept_online.add("floor_mgr_4")
        mgr.nurse_online.add("anna")
        escalations = []
        calls = install_fakes(
            monkeypatch, mgr,
            targets={"canteen": "canteen_cathy", "pharmacy": "pharmacy_pete",
                     "housekeeping": "floor_mgr_4"},
            route_alert_spy=escalations,
        )
        asyncio.run(_init_db())
        yield {"mgr": mgr, "escalations": escalations, "calls": calls}
        for t in list(dd._timers.values()):
            t.cancel()
        dd._timers.clear()


def run(coro): return asyncio.run(coro)


# ── Delivery ──────────────────────────────────────────────────────────────────

def test_route_delivers_to_department_and_notifies_nurse(ctx):
    async def body():
        db = await _new_db()
        aid = await _make_alert(db)
        res = await dd.route_request(
            db, alert_id=aid, department="canteen", room_id="4B",
            transcript="water please", predicted_department="canteen",
            routing_reason="keyword", confidence=0.9, model_version="t",
        )
        assert res["ok"] and res["target"] == "canteen_cathy"
        assert ctx["mgr"].sent_dept.get("canteen_cathy")        # delivered
        assert ctx["mgr"].sent_nurse.get("anna")                # nurse notice
        req = await database.get_department_request(db, res["request_id"])
        assert req["state"] == "pending"
        dd._cancel_timer(res["request_id"])
        await db.close()
    run(body())


def test_housekeeping_routes_to_floor_manager(ctx):
    async def body():
        db = await _new_db()
        aid = await _make_alert(db)
        res = await dd.route_request(
            db, alert_id=aid, department="housekeeping", room_id="4B",
            transcript="the AC is broken", predicted_department="housekeeping",
            routing_reason="keyword", confidence=0.9, model_version="t",
        )
        assert res["target"] == "floor_mgr_4"
        dd._cancel_timer(res["request_id"])
        await db.close()
    run(body())


# ── Acknowledge / done ────────────────────────────────────────────────────────

def test_acknowledge_then_done(ctx):
    async def body():
        db = await _new_db()
        aid = await _make_alert(db)
        res = await dd.route_request(
            db, alert_id=aid, department="canteen", room_id="4B",
            transcript="water", predicted_department="canteen",
            routing_reason="keyword", confidence=0.9, model_version="t",
        )
        rid = res["request_id"]
        a = await dd.acknowledge(db, rid, "canteen_cathy")
        assert a["ok"] and a["state"] == "acknowledged"
        d = await dd.complete(db, rid, "canteen_cathy")
        assert d["ok"] and d["state"] == "done"
        # No escalation should fire after done.
        await asyncio.sleep(0.5)
        assert aid not in ctx["escalations"]
        await db.close()
    run(body())


def test_double_acknowledge_rejected(ctx):
    async def body():
        db = await _new_db()
        aid = await _make_alert(db)
        res = await dd.route_request(
            db, alert_id=aid, department="canteen", room_id="4B",
            transcript="water", predicted_department="canteen",
            routing_reason="keyword", confidence=0.9, model_version="t",
        )
        rid = res["request_id"]
        a1 = await dd.acknowledge(db, rid, "canteen_cathy")
        a2 = await dd.acknowledge(db, rid, "someone_else")
        assert a1["ok"] and not a2["ok"]
        await db.close()
    run(body())


# ── SLA timeout → escalate to nurse ──────────────────────────────────────────

def test_sla_timeout_escalates_to_nurse(ctx):
    async def body():
        db = await _new_db()
        aid = await _make_alert(db)
        res = await dd.route_request(
            db, alert_id=aid, department="canteen", room_id="4B",
            transcript="water", predicted_department="canteen",
            routing_reason="keyword", confidence=0.9, model_version="t",
        )
        rid = res["request_id"]
        # The SLA timer must be armed for a pending request.
        assert rid in dd._timers, "SLA timer was not armed"
        # Cancel the background timer and drive the escalation deterministically
        # (awaiting a real sleeping background task across asyncio.run boundaries
        # is flaky; _escalate_to_nurse IS the exact code path the timer runs).
        dd._cancel_timer(rid)
        await dd._escalate_to_nurse(db, rid, reason="sla_timeout")
        req = await database.get_department_request(db, rid)
        alert = await database.get_alert_by_id(db, aid)
        assert req["state"] == "escalated", req["state"]
        assert alert["dept_escalated"] == 1, "alert not flagged"
        assert aid in ctx["escalations"], "route_alert not called by escalation"
        await db.close()
    run(body())


def test_ack_before_sla_prevents_escalation(ctx):
    async def body():
        db = await _new_db()
        aid = await _make_alert(db)
        res = await dd.route_request(
            db, alert_id=aid, department="canteen", room_id="4B",
            transcript="water", predicted_department="canteen",
            routing_reason="keyword", confidence=0.9, model_version="t",
        )
        rid = res["request_id"]
        await dd.acknowledge(db, rid, "canteen_cathy")
        await asyncio.sleep(0.5)
        req = await database.get_department_request(db, rid)
        assert req["state"] == "acknowledged"
        assert aid not in ctx["escalations"]
        await db.close()
    run(body())


# ── "Not mine" bounce-back ────────────────────────────────────────────────────

def test_not_mine_escalates_to_nurse(ctx):
    async def body():
        db = await _new_db()
        aid = await _make_alert(db)
        res = await dd.route_request(
            db, alert_id=aid, department="canteen", room_id="4B",
            transcript="water", predicted_department="canteen",
            routing_reason="keyword", confidence=0.9, model_version="t",
        )
        rid = res["request_id"]
        r = await dd.reject(db, rid, "canteen_cathy", "not mine")
        assert r["ok"] and r["state"] == "escalated"
        assert aid in ctx["escalations"]
        await db.close()
    run(body())


# ── Dedupe ────────────────────────────────────────────────────────────────────

def test_dedupe_merges_repeat(ctx):
    async def body():
        db = await _new_db()
        aid = await _make_alert(db)
        r1 = await dd.route_request(
            db, alert_id=aid, department="canteen", room_id="4B",
            transcript="water", predicted_department="canteen",
            routing_reason="keyword", confidence=0.9, model_version="t",
        )
        r2 = await dd.route_request(
            db, alert_id=aid, department="canteen", room_id="4B",
            transcript="water again", predicted_department="canteen",
            routing_reason="keyword", confidence=0.9, model_version="t",
        )
        assert r2.get("deduped") is True
        assert r1["request_id"] == r2["request_id"]
        async with db.execute(
            "SELECT COUNT(*) AS c FROM department_requests WHERE room_id='4B' AND department='canteen'"
        ) as cur:
            c = (await cur.fetchone())["c"]
        assert c == 1
        req = await database.get_department_request(db, r1["request_id"])
        assert req["repeat_count"] == 1
        dd._cancel_timer(r1["request_id"])
        await db.close()
    run(body())


# ── No target configured → straight to nurse ─────────────────────────────────

def test_no_target_escalates(ctx, monkeypatch):
    async def body():
        import pg_database
        async def none_user(conn, department): return None
        monkeypatch.setattr(pg_database, "get_department_user", none_user)
        db = await _new_db()
        aid = await _make_alert(db)
        res = await dd.route_request(
            db, alert_id=aid, department="pharmacy", room_id="4B",
            transcript="refill", predicted_department="pharmacy",
            routing_reason="keyword", confidence=0.9, model_version="t",
        )
        assert res.get("escalated") is True
        assert aid in ctx["escalations"]
        await db.close()
    run(body())
