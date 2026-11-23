"""
Rigorous tests for the Uber-style dispatch engine (dispatch.py) and its durable
SQLite state machine (database.py).

Design of the harness
----------------------
dispatch.py has three external seams we don't want to spin up for a unit test:
  1. the nurse-alert WebSocket ConnectionManager (routers.ws.manager),
  2. the signaling registry (routers.signal.registry),
  3. the Postgres nurse routing map (pg_database.get_nurse_routing_map / get_conn).

We replace all three with in-memory fakes so we can exercise EVERY branch and
edge case deterministically against a real temporary SQLite database (the
authoritative lifecycle state). Timers are shortened so the 10s/15s windows
don't make the suite slow.

Run:  python -m pytest tests/test_dispatch.py -v
(from the server/ directory; no pytest-asyncio needed — we drive asyncio.run.)
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import aiosqlite
import pytest

# Make the server package importable when run from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

import database  # noqa: E402
import dispatch  # noqa: E402
import routing   # noqa: E402


# ── Fakes ─────────────────────────────────────────────────────────────────────

class FakeWsManager:
    """Stands in for routers.ws.manager."""
    def __init__(self):
        self.online: set[str] = set()
        self.admins_online = False
        self.sent: dict[str, list] = {}       # nurse -> [frames]
        self.admin_frames: list = []

    def is_online(self, nurse: str) -> bool:
        return nurse in self.online

    async def send_to_nurse(self, nurse: str, message: str) -> bool:
        if nurse not in self.online:
            return False
        self.sent.setdefault(nurse, []).append(message)
        return True

    async def send_to_admins(self, message: str) -> None:
        self.admin_frames.append(message)

    async def broadcast(self, message: str) -> None:
        for n in self.online:
            self.sent.setdefault(n, []).append(message)


class FakeSignalRegistry:
    def __init__(self):
        self.in_call: set[str] = set()

    def is_busy(self, username: str) -> bool:
        return username in self.in_call


class FakeWsModule:
    def __init__(self, manager):
        self.manager = manager


# ── Harness ───────────────────────────────────────────────────────────────────

class Harness:
    def __init__(self, tmpdir):
        self.db_path = os.path.join(tmpdir, "test.db")
        self.ws = FakeWsManager()
        self.signal = FakeSignalRegistry()
        # username -> {full_name, ward, ...}
        self.routing_map: dict[str, dict] = {}

    def nurse(self, username, online=True, in_call=False, full_name=None, ward="Ward A"):
        self.routing_map[username] = {
            "full_name": full_name or username, "ward": ward,
            "competencies": set(), "status": "available", "is_supervisor": False,
        }
        if online:
            self.ws.online.add(username)
        if in_call:
            self.signal.in_call.add(username)

    def set_online(self, username, online):
        if online:
            self.ws.online.add(username)
        else:
            self.ws.online.discard(username)


def install_fakes(monkeypatch, h: Harness):
    """Point every seam at the harness's fakes and the temp DB."""
    config.settings.DB_PATH = Path(h.db_path)

    fake_ws_module = FakeWsModule(h.ws)
    # dispatch/routing import `from routers import ws as ws_router`
    monkeypatch.setitem(sys.modules, "routers.ws", fake_ws_module)

    # Signaling registry: `from routers.signal import registry`
    fake_signal_module = type(sys)("routers.signal")
    fake_signal_module.registry = h.signal
    monkeypatch.setitem(sys.modules, "routers.signal", fake_signal_module)

    # Postgres routing map + get_conn
    async def fake_get_conn():
        yield object()  # unused; get_nurse_routing_map is also stubbed

    async def fake_get_nurse_routing_map(conn):
        return dict(h.routing_map)

    async def fake_get_nurses_for_room(conn, room_number):
        # Primary = the nurse whose patient is this room; the tests set an
        # explicit mapping on the harness.
        return h.room_nurses.get(room_number, [])

    import pg_database
    monkeypatch.setattr(pg_database, "get_conn", fake_get_conn)
    monkeypatch.setattr(pg_database, "get_nurse_routing_map", fake_get_nurse_routing_map)
    monkeypatch.setattr(pg_database, "get_nurses_for_room", fake_get_nurses_for_room)

    # Reset manual-busy + any live timers between tests.
    routing.MANUAL_BUSY.clear()
    for t in list(dispatch._timers.values()):
        t.cancel()
    dispatch._timers.clear()

    # Speed up the windows so the suite is fast but logic is identical.
    monkeypatch.setattr(dispatch, "BROADCAST_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(dispatch, "PRIMARY_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(dispatch, "OFFLINE_RECLAIM_GRACE_SECONDS", 0.0)


async def _init_db():
    await database.init_db()


async def _new_db():
    db = await aiosqlite.connect(config.settings.DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA busy_timeout=5000")
    return db


async def _make_alert(db, room="4B", priority="Critical", intent="Emergency"):
    aid = await database.insert_alert(
        db, room_id=room, priority=priority, intent=intent,
        distress_score=0.0, transcript="help", wav_path=None,
        patient_name="Test Patient", summary="needs help",
    )
    await db.commit()
    return await database.get_alert_by_id(db, aid)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def harness(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(tmp)
        h.room_nurses = {}
        install_fakes(monkeypatch, h)
        asyncio.run(_init_db())
        yield h
        for t in list(dispatch._timers.values()):
            t.cancel()
        dispatch._timers.clear()


def run(coro):
    return asyncio.run(coro)


# ── Tests: happy path ───────────────────────────────────────────────────────-

def test_primary_available_gets_offer(harness):
    h = harness
    h.nurse("anna"); h.nurse("ben")
    h.room_nurses["4B"] = ["anna"]

    async def body():
        db = await _new_db()
        alert = await _make_alert(db)
        res = await dispatch.start_dispatch(db, alert)
        assert res["state"] == "pending_primary"
        assert res["primary"] == "anna"
        # Anna got exactly one frame (assigned to her); ben got none.
        assert len(h.ws.sent.get("anna", [])) == 1
        assert "ben" not in h.ws.sent
        row = await database.get_dispatch_by_alert(db, alert["id"])
        # Hers straight away — no Accept step and no timer that takes it back.
        assert row["state"] == "assigned" and row["assignee"] == "anna"
        assert row["id"] not in dispatch._timers
        await db.close()
    run(body())


def test_second_request_goes_to_other_free_nurse(harness):
    h = harness
    h.nurse("anna"); h.nurse("ben")
    h.room_nurses["4B"] = ["anna"]

    async def body():
        db = await _new_db()
        a1 = await _make_alert(db)
        await dispatch.start_dispatch(db, a1)           # -> anna (now busy holding it)
        a2 = await _make_alert(db)
        res = await dispatch.start_dispatch(db, a2)
        assert res["state"] == "broadcasting"
        assert res["delivered"] == ["ben"]              # free nurse, not the busy primary
        # The first request is still Anna's — nothing was taken away or acked.
        r1 = await database.get_dispatch_by_alert(db, a1["id"])
        assert r1["state"] == "assigned" and r1["assignee"] == "anna"
        assert (await database.get_alert_by_id(db, a1["id"]))["acknowledged"] == 0
        for rid in list(dispatch._timers):
            dispatch._cancel_timer(rid)
        await db.close()
    run(body())


def test_primary_accepts(harness):
    h = harness
    h.nurse("anna"); h.room_nurses["4B"] = ["anna"]

    async def body():
        db = await _new_db()
        alert = await _make_alert(db)
        await dispatch.start_dispatch(db, alert)
        row = await database.get_dispatch_by_alert(db, alert["id"])
        res = await dispatch.accept(db, row["id"], "anna")
        assert res["ok"] is True and res["assignee"] == "anna"
        row2 = await database.get_dispatch_request(db, row["id"])
        assert row2["state"] == "assigned"
        await db.close()
    run(body())


def test_decline_broadcasts_to_free_only(harness):
    h = harness
    h.nurse("anna"); h.nurse("ben"); h.nurse("carol", in_call=True)  # carol busy
    h.room_nurses["4B"] = ["anna"]

    async def body():
        db = await _new_db()
        alert = await _make_alert(db)
        await dispatch.start_dispatch(db, alert)
        row = await database.get_dispatch_by_alert(db, alert["id"])
        res = await dispatch.decline(db, row["id"], "anna", "engaged with another patient")
        assert res["ok"] is True
        # Broadcast went to ben (free) but NOT carol (busy/on call) nor anna.
        assert "ben" in res["delivered"]
        assert "carol" not in res["delivered"]
        assert "anna" not in res["delivered"]
        row2 = await database.get_dispatch_request(db, row["id"])
        assert row2["state"] == "broadcasting"
        assert "engaged" in (row2["decline_reason"] or "")
        await db.close()
    run(body())


def test_no_available_primary_broadcasts_immediately(harness):
    h = harness
    h.nurse("anna", in_call=True)   # primary busy
    h.nurse("ben")                  # free
    h.room_nurses["4B"] = ["anna"]

    async def body():
        db = await _new_db()
        alert = await _make_alert(db)
        res = await dispatch.start_dispatch(db, alert)
        assert res["state"] == "broadcasting"
        assert "ben" in res["delivered"]
        await db.close()
    run(body())


# ── Tests: concurrency / single-winner ─────────────────────────────────────-

def test_two_accepts_single_winner(harness):
    """The atomic single-winner guarantee comes from the SQL
    UPDATE ... WHERE state open AND assignee open clause: whichever write lands
    first flips the row to 'assigned' and the other's WHERE no longer matches.
    We exercise both orderings to prove exactly one caller wins."""
    h = harness
    h.nurse("anna", in_call=True)  # busy primary → straight to broadcast
    h.nurse("ben"); h.nurse("carol")
    h.room_nurses["4B"] = ["anna"]

    async def body():
        db = await _new_db()
        alert = await _make_alert(db)
        await dispatch.start_dispatch(db, alert)
        row = await database.get_dispatch_by_alert(db, alert["id"])
        # Fire both accepts back-to-back on the same loop. The claim UPDATE
        # serialises; the second finds the row already assigned → loses.
        r1 = await dispatch.accept(db, row["id"], "ben")
        r2 = await dispatch.accept(db, row["id"], "carol")
        wins = [r for r in (r1, r2) if r.get("ok")]
        losses = [r for r in (r1, r2) if not r.get("ok")]
        assert len(wins) == 1, (r1, r2)
        assert len(losses) == 1
        assert losses[0]["error"] == "already_taken"
        assert wins[0]["assignee"] == "ben"   # ben's claim landed first
        await db.close()
    run(body())


def test_claim_race_direct(harness):
    """Lower-level proof: two claim_dispatch_request calls, only one rowcount>0."""
    h = harness
    h.nurse("ben"); h.nurse("carol")

    async def body():
        db = await _new_db()
        alert = await _make_alert(db)
        rid = await database.create_dispatch_request(
            db, alert_id=alert["id"], room_id="4B", primary_nurse=None,
            initial_state="broadcasting",
        )
        won1, _ = await database.claim_dispatch_request(db, rid, "ben")
        won2, _ = await database.claim_dispatch_request(db, rid, "carol")
        assert won1 is True and won2 is False
        await db.close()
    run(body())


# ── Tests: timeout → admin ──────────────────────────────────────────────────-

def test_timeout_escalates_to_admin(harness):
    h = harness
    h.nurse("anna", in_call=True)   # busy primary → broadcast
    h.nurse("ben")                  # free but will NOT accept
    h.room_nurses["4B"] = ["anna"]

    async def body():
        db = await _new_db()
        alert = await _make_alert(db)
        await dispatch.start_dispatch(db, alert)
        row = await database.get_dispatch_by_alert(db, alert["id"])
        assert row["state"] == "broadcasting"
        # Wait past the (shortened) 10s window.
        await asyncio.sleep(0.6)
        row2 = await database.get_dispatch_request(db, row["id"])
        assert row2["state"] == "needs_admin"
        assert len(h.ws.admin_frames) >= 1
        await db.close()
    run(body())


def test_no_free_nurse_escalates_immediately(harness):
    h = harness
    h.nurse("anna", in_call=True)   # busy primary
    h.nurse("ben", in_call=True)    # busy
    h.room_nurses["4B"] = ["anna"]

    async def body():
        db = await _new_db()
        alert = await _make_alert(db)
        res = await dispatch.start_dispatch(db, alert)
        # No free nurse → should escalate to admin without waiting.
        row = await database.get_dispatch_by_alert(db, alert["id"])
        assert row["state"] == "needs_admin"
        assert len(h.ws.admin_frames) >= 1
        await db.close()
    run(body())


def test_accept_before_timeout_prevents_admin(harness):
    h = harness
    h.nurse("anna", in_call=True)
    h.nurse("ben")
    h.room_nurses["4B"] = ["anna"]

    async def body():
        db = await _new_db()
        alert = await _make_alert(db)
        await dispatch.start_dispatch(db, alert)
        row = await database.get_dispatch_by_alert(db, alert["id"])
        await dispatch.accept(db, row["id"], "ben")
        await asyncio.sleep(0.6)  # let any stray timer fire
        row2 = await database.get_dispatch_request(db, row["id"])
        assert row2["state"] == "assigned" and row2["assignee"] == "ben"
        assert len(h.ws.admin_frames) == 0
        await db.close()
    run(body())


# ── Tests: admin reassign (available only) ──────────────────────────────────-

def test_admin_reassign_to_available(harness):
    h = harness
    h.nurse("anna", in_call=True)
    h.nurse("ben", in_call=True)
    h.room_nurses["4B"] = ["anna"]

    async def body():
        db = await _new_db()
        alert = await _make_alert(db)
        await dispatch.start_dispatch(db, alert)   # → needs_admin (no free)
        row = await database.get_dispatch_by_alert(db, alert["id"])
        assert row["state"] == "needs_admin"
        # Ben frees up; admin reassigns to ben.
        h.signal.in_call.discard("ben")
        res = await dispatch.admin_reassign(db, row["id"], "ben")
        assert res["ok"] is True and res["assignee"] == "ben"
        await db.close()
    run(body())


def test_admin_reassign_to_unavailable_rejected(harness):
    h = harness
    h.nurse("anna", in_call=True)
    h.nurse("ben", in_call=True)   # still busy
    h.room_nurses["4B"] = ["anna"]

    async def body():
        db = await _new_db()
        alert = await _make_alert(db)
        await dispatch.start_dispatch(db, alert)
        row = await database.get_dispatch_by_alert(db, alert["id"])
        res = await dispatch.admin_reassign(db, row["id"], "ben")
        assert res["ok"] is False and res["error"] == "nurse_unavailable"
        await db.close()
    run(body())


def test_admin_reassign_to_offline_rejected(harness):
    h = harness
    h.nurse("anna", in_call=True)
    h.nurse("ben", online=False)   # offline
    h.room_nurses["4B"] = ["anna"]

    async def body():
        db = await _new_db()
        alert = await _make_alert(db)
        await dispatch.start_dispatch(db, alert)
        row = await database.get_dispatch_by_alert(db, alert["id"])
        res = await dispatch.admin_reassign(db, row["id"], "ben")
        assert res["ok"] is False and res["error"] == "nurse_unavailable"
        await db.close()
    run(body())


# ── Tests: logout / offline presence ────────────────────────────────────────-

def test_logged_out_nurse_is_offline_not_free(harness):
    h = harness
    h.nurse("anna")   # online, free

    async def body():
        db = await _new_db()
        assert await dispatch.compute_status(db, "anna") == dispatch.STATUS_AVAILABLE
        # She logs out → socket gone.
        h.set_online("anna", False)
        assert await dispatch.compute_status(db, "anna") == dispatch.STATUS_OFFLINE
        assert routing.nurse_is_online("anna") is False
        # And she is NOT in the available pool.
        free = await dispatch.available_nurses(db)
        assert "anna" not in free
        await db.close()
    run(body())


def test_reclaim_from_offline_assignee(harness, monkeypatch):
    h = harness
    h.nurse("anna", in_call=True)
    h.nurse("ben"); h.nurse("carol")
    h.room_nurses["4B"] = ["anna"]
    # Make reclaim instant (no grace sleep) for the test.

    async def body():
        db = await _new_db()
        alert = await _make_alert(db)
        await dispatch.start_dispatch(db, alert)       # broadcast to ben, carol
        row = await database.get_dispatch_by_alert(db, alert["id"])
        await dispatch.accept(db, row["id"], "ben")    # ben wins
        # Ben logs out.
        h.set_online("ben", False)
        await dispatch.reclaim_from_offline_nurse(db, "ben")
        row2 = await database.get_dispatch_request(db, row["id"])
        assert row2["state"] == "broadcasting"          # re-broadcast to free
        await db.close()
    run(body())


# ── Tests: idempotency / stale clients ──────────────────────────────────────-

def test_double_accept_is_idempotent(harness):
    h = harness
    h.nurse("anna", in_call=True); h.nurse("ben")
    h.room_nurses["4B"] = ["anna"]

    async def body():
        db = await _new_db()
        alert = await _make_alert(db)
        await dispatch.start_dispatch(db, alert)
        row = await database.get_dispatch_by_alert(db, alert["id"])
        r1 = await dispatch.accept(db, row["id"], "ben")
        r2 = await dispatch.accept(db, row["id"], "ben")  # double-tap
        assert r1["ok"] and r2["ok"]
        await db.close()
    run(body())


def test_accept_after_ack_is_rejected(harness):
    h = harness
    h.nurse("anna", in_call=True); h.nurse("ben")
    h.room_nurses["4B"] = ["anna"]

    async def body():
        db = await _new_db()
        alert = await _make_alert(db)
        await dispatch.start_dispatch(db, alert)
        row = await database.get_dispatch_by_alert(db, alert["id"])
        # Alert gets acknowledged out of band.
        await database.acknowledge_alert(db, alert["id"], ack_by="someone")
        await dispatch.on_alert_acknowledged(db, alert["id"], "someone")
        res = await dispatch.accept(db, row["id"], "ben")
        assert res["ok"] is False
        assert res["error"] in ("already_handled", "already_resolved")
        await db.close()
    run(body())


def test_decline_by_wrong_nurse_rejected(harness):
    h = harness
    h.nurse("anna"); h.nurse("ben")
    h.room_nurses["4B"] = ["anna"]

    async def body():
        db = await _new_db()
        alert = await _make_alert(db)
        await dispatch.start_dispatch(db, alert)   # offered to anna (primary)
        row = await database.get_dispatch_by_alert(db, alert["id"])
        res = await dispatch.decline(db, row["id"], "ben", "nope")  # ben isn't the offeree
        assert res["ok"] is False and res["error"] == "not_your_request"
        await db.close()
    run(body())


def test_ack_resolves_and_cancels_timer(harness):
    h = harness
    h.nurse("anna", in_call=True); h.nurse("ben")
    h.room_nurses["4B"] = ["anna"]

    async def body():
        db = await _new_db()
        alert = await _make_alert(db)
        await dispatch.start_dispatch(db, alert)
        row = await database.get_dispatch_by_alert(db, alert["id"])
        await database.acknowledge_alert(db, alert["id"], ack_by="ben")
        await dispatch.on_alert_acknowledged(db, alert["id"], "ben")
        assert row["id"] not in dispatch._timers      # timer cancelled
        row2 = await database.get_dispatch_request(db, row["id"])
        assert row2["state"] == "resolved"
        await asyncio.sleep(0.6)                        # ensure no late admin escalation
        assert len(h.ws.admin_frames) == 0
        await db.close()
    run(body())


def test_primary_unresponsive_falls_through(harness):
    h = harness
    h.nurse("anna"); h.nurse("ben")
    h.room_nurses["4B"] = ["anna"]

    async def body():
        db = await _new_db()
        alert = await _make_alert(db)
        res = await dispatch.start_dispatch(db, alert)
        assert res["state"] == "pending_primary"
        # Anna never responds; primary timer should broadcast to ben.
        await asyncio.sleep(0.6)
        row = await database.get_dispatch_request(db, (await database.get_dispatch_by_alert(db, alert["id"]))["id"])
        assert row["state"] in ("broadcasting", "needs_admin", "assigned")
        assert row["state"] != "pending_primary"
        await db.close()
    run(body())


def test_duplicate_dispatch_same_alert_one_row(harness):
    h = harness
    h.nurse("anna"); h.room_nurses["4B"] = ["anna"]

    async def body():
        db = await _new_db()
        alert = await _make_alert(db)
        await dispatch.start_dispatch(db, alert)
        await dispatch.start_dispatch(db, alert)   # re-route same alert
        async with db.execute(
            "SELECT COUNT(*) AS c FROM dispatch_requests WHERE alert_id = ?", (alert["id"],)
        ) as cur:
            cnt = (await cur.fetchone())["c"]
        assert cnt == 1
        await db.close()
    run(body())
