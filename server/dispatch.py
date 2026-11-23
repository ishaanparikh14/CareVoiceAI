"""
dispatch.py — Uber-style request dispatch for CareVoice AI.

This module owns the live routing of a nurse-call alert through a deterministic
state machine, replacing the old "send to assigned nurses, else broadcast"
behaviour in routing.py.

Flow
----
1.  A new alert enters via `start_dispatch`.
2.  It is offered to the patient's PRIMARY (attending) nurse if she is
    AVAILABLE. Otherwise it broadcasts immediately to every FREE nurse.
3.  If the primary DECLINES ("not available" / "engaged with other patients")
    the request broadcasts to every FREE nurse and a 10-second timer starts.
4.  The first FREE nurse to ACCEPT wins (atomic single-winner claim in SQLite).
5.  If no free nurse accepts within the window, the request moves to
    `needs_admin` and a notification is pushed to every connected admin so the
    admin can reassign — but only among nurses that are AVAILABLE right now.

Durability / concurrency
-------------------------
The authoritative lifecycle state lives in SQLite (`dispatch_requests`), so a
reconnecting nurse or a polling admin always sees the truth and two concurrent
accepts can never both win. The 10-second timers are in-memory asyncio tasks
keyed by request id (one per request, cancelled on resolution). On startup the
server re-arms timers for any request left mid-broadcast, so a restart does not
strand a request (see `rearm_pending_timers`).

Single-process assumption: like ws.py / signal.py this assumes one event loop /
one worker. For a multi-worker deployment the timers + presence must move to a
shared store (e.g. Redis); the DB state machine already is shared.
"""

from __future__ import annotations

import asyncio
import logging

import aiosqlite

from config import settings

logger = logging.getLogger(__name__)

# Seconds a broadcast waits for a free nurse to accept before escalating to the
# admin. The product spec calls for 10s.
BROADCAST_TIMEOUT_SECONDS = 10

# How long the primary nurse has to accept/decline before the request auto-falls
# through to a broadcast (so an unresponsive primary never strands a patient).
PRIMARY_TIMEOUT_SECONDS = 15

# Grace before reclaiming a request from a nurse who just went offline, so a
# brief socket flap doesn't needlessly re-broadcast.
OFFLINE_RECLAIM_GRACE_SECONDS = 1.0

# request_id -> asyncio.Task running the active timeout (broadcast or primary).
# One per request; replaced/cancelled on every state transition.
_timers: dict[int, asyncio.Task] = {}


# ── Nurse status (explicit, presence-aware) ───────────────────────────────────
# A nurse's dispatch status is derived, not just stored, so logout/disconnect is
# authoritative:
#   OFFLINE   — no live nurse-alert WebSocket (logged out / app closed / network)
#   BUSY      — online but on a call, holding an assigned request/alert, or
#               manually marked busy by themselves or the admin
#   AVAILABLE — online and not busy
#
# Presence comes ONLY from the nurse-alert ConnectionManager (ws.py), never the
# signaling registry. That fixes the "logged-out nurse still shows FREE" bug:
# logout cancels the alert socket, so the nurse is immediately OFFLINE even if a
# signaling socket lingers.

STATUS_AVAILABLE = "available"
STATUS_BUSY = "busy"
STATUS_OFFLINE = "offline"


def _presence_online(nurse: str) -> bool:
    from routers import ws as ws_router
    return ws_router.manager.is_online(nurse)


async def compute_status(
    db: aiosqlite.Connection, nurse: str, *, active_counts: dict[str, int] | None = None
) -> str:
    """Return the derived dispatch status for one nurse."""
    if not _presence_online(nurse):
        return STATUS_OFFLINE
    from routing import MANUAL_BUSY
    from routers.signal import registry as signal_registry
    from database import count_active_dispatch_by_nurse, get_active_alert_counts_by_nurse

    if active_counts is None:
        alert_counts = await get_active_alert_counts_by_nurse(db)
        disp_counts = await count_active_dispatch_by_nurse(db)
    else:
        alert_counts = active_counts
        disp_counts = await count_active_dispatch_by_nurse(db)

    on_call = signal_registry.is_busy(nurse)
    holding = alert_counts.get(nurse, 0) > 0 or disp_counts.get(nurse, 0) > 0
    manual = nurse in MANUAL_BUSY
    if on_call or holding or manual:
        return STATUS_BUSY
    return STATUS_AVAILABLE


async def available_nurses(db: aiosqlite.Connection, *, exclude: set[str] | None = None) -> list[str]:
    """Usernames of every approved nurse currently AVAILABLE (online + free).

    This is the single source of truth for "FREE nurses" used by both the
    broadcast fan-out and the admin reassign dropdown, so the two can never
    disagree about who may receive a request."""
    from pg_database import get_conn, get_nurse_routing_map
    from database import get_active_alert_counts_by_nurse, count_active_dispatch_by_nurse

    exclude = exclude or set()
    async for conn in get_conn():
        routing_map = await get_nurse_routing_map(conn)
        break

    alert_counts = await get_active_alert_counts_by_nurse(db)
    disp_counts = await count_active_dispatch_by_nurse(db)
    from routing import MANUAL_BUSY
    from routers.signal import registry as signal_registry

    free: list[str] = []
    for u in routing_map:
        if u in exclude:
            continue
        if not _presence_online(u):
            continue
        if signal_registry.is_busy(u):
            continue
        if alert_counts.get(u, 0) > 0 or disp_counts.get(u, 0) > 0:
            continue
        if u in MANUAL_BUSY:
            continue
        free.append(u)
    return free


async def is_available(db: aiosqlite.Connection, nurse: str) -> bool:
    """True if this one nurse is AVAILABLE right now (used to validate accepts
    and admin reassignments at click time, closing the poll-vs-click race)."""
    return await compute_status(db, nurse) == STATUS_AVAILABLE


# ── WebSocket payload builders ────────────────────────────────────────────────

def _dispatch_frame(event: str, *, request_id: int, alert: dict, extra: dict | None = None) -> dict:
    """Build a dispatch WS frame the nurse app understands. Carries the full
    alert fields (so the dashboard can render the card) plus dispatch metadata."""
    frame = {
        "event": event,                       # dispatch_request | dispatch_assigned | dispatch_cancelled
        "request_id": request_id,
        "alert_id": alert["id"],
        "room_id": alert["room_id"],
        "priority": alert["priority"],
        "intent": alert["intent"],
        "transcript": alert["transcript"],
        "created_at": alert["created_at"],
        "patient_name": alert.get("patient_name"),
        "summary": alert.get("summary"),
        "emotion": alert.get("emotion"),
        "escalated": bool(alert.get("escalated") or 0),
        "distress_score": alert.get("distress_score", 0.0),
    }
    if extra:
        frame.update(extra)
    return frame


async def _send_to(nurse: str, frame: dict) -> bool:
    import json
    from routers import ws as ws_router
    return await ws_router.manager.send_to_nurse(nurse, json.dumps(frame))


async def _broadcast_free(db: aiosqlite.Connection, frame: dict, *, exclude: set[str] | None = None) -> list[str]:
    """Push a frame to every FREE nurse. Returns the list actually delivered to."""
    import json
    from routers import ws as ws_router
    targets = await available_nurses(db, exclude=exclude)
    delivered: list[str] = []
    text = json.dumps(frame)
    for n in targets:
        if await ws_router.manager.send_to_nurse(n, text):
            delivered.append(n)
    return delivered


async def notify_admins(frame: dict) -> None:
    import json
    from routers import ws as ws_router
    await ws_router.manager.send_to_admins(json.dumps(frame))


async def push_alert_update(db: aiosqlite.Connection, alert_id: int, text: str) -> list[str]:
    """Deliver a server-side change to an alert (e.g. Urgent -> Critical) ONLY
    to whoever holds it under the dispatch rules — never to every nurse:
      pending_primary / assigned -> the offered / assigned nurse
      broadcasting / needs_admin -> the FREE nurses (a busy nurse is never free)
      no dispatch row (legacy)   -> the room's own nurses who are online
    Admins always receive it. Returns the nurses it went to."""
    from database import get_dispatch_by_alert, get_alert_by_id
    from routers import ws as ws_router
    sent: list[str] = []
    row = await get_dispatch_by_alert(db, alert_id)
    if row is None:
        alert = await get_alert_by_id(db, alert_id)
        if alert is not None:
            from pg_database import get_conn, get_nurses_for_room
            async for conn in get_conn():
                nurses = await get_nurses_for_room(conn, alert["room_id"])
                break
            for n in nurses:
                if await ws_router.manager.send_to_nurse(n, text):
                    sent.append(n)
    elif row["state"] in ("pending_primary", "assigned") and row["assignee"]:
        if await ws_router.manager.send_to_nurse(row["assignee"], text):
            sent.append(row["assignee"])
    elif row["state"] in ("broadcasting", "needs_admin"):
        for n in await available_nurses(db):
            if await ws_router.manager.send_to_nurse(n, text):
                sent.append(n)
    await ws_router.manager.send_to_admins(text)
    return sent


async def nurse_can_see(db: aiosqlite.Connection, nurse: str, my_rooms: set[str],
                        alert: dict, row: dict | None, *, i_am_free: bool) -> bool:
    """Whether an alert belongs on THIS nurse's dashboard (poll + reconnect).

    Mirrors the live dispatch so the list never shows a nurse a request the
    engine did not give her:
      offered to / assigned to / accepted by her        -> yes
      broadcasting, she is FREE and not the busy primary -> yes (she may take it)
      assigned to another nurse                          -> no
      needs admin reassignment                           -> no (admin assigns)
      finished (resolved/cancelled)                      -> history, if she handled
                                                            it or it is her patient
      no dispatch row (not paged, older alerts)          -> her own rooms only
    """
    room = (alert.get("room_id") or "").strip()
    if row is None:
        return room in my_rooms
    state, assignee = row["state"], row["assignee"]
    if assignee == nurse and state not in ("resolved", "cancelled"):
        return True
    if state in ("broadcasting", "needs_admin"):
        # Unclaimed: every FREE nurse keeps seeing it until someone takes it,
        # even after it is also flagged to the admin ("Needs reassignment").
        # A busy nurse (including the busy primary) does not.
        return i_am_free
    if state in ("resolved", "cancelled"):
        return room in my_rooms and assignee in (None, nurse)
    return False


async def rooms_handled_by(db: aiosqlite.Connection, nurse: str) -> set[str]:
    """Rooms a nurse is TEMPORARILY mapped to: she holds an active (unresolved)
    request for them (accepted broadcast or admin reassignment)."""
    async with db.execute(
        "SELECT DISTINCT room_id FROM dispatch_requests WHERE assignee = ? "
        "AND state NOT IN ('resolved','cancelled')", (nurse,)) as cur:
        return {r[0] for r in await cur.fetchall() if r[0]}


# ── Lifecycle ─────────────────────────────────────────────────────────────────

async def start_dispatch(db: aiosqlite.Connection, alert: dict) -> dict:
    """Entry point: route a freshly created alert.

    Offers to the primary nurse if she is AVAILABLE; otherwise broadcasts to all
    FREE nurses immediately. Returns a small status dict for logging/response.
    """
    from pg_database import get_conn, get_nurses_for_room
    from database import create_dispatch_request, record_reroute, set_alert_routed_to

    alert_id = alert["id"]
    room_id = alert["room_id"]

    async for conn in get_conn():
        assigned = await get_nurses_for_room(conn, room_id)
        break
    primary = assigned[0] if assigned else None

    # Decide the opening move.
    if primary and await is_available(db, primary):
        # The patient's own nurse is online and free: the request is HERS
        # straight away (no Accept step, no timer). It stays on her screen until
        # she acknowledges it. Only if she goes offline is it re-broadcast
        # (reclaim_from_offline_nurse). While she holds it she counts as busy,
        # so the patient's next request goes to the other free nurses.
        req_id = await create_dispatch_request(
            db, alert_id=alert_id, room_id=room_id,
            primary_nurse=primary, initial_state="assigned", assignee=primary,
        )
        await set_alert_routed_to(db, alert_id, primary, False)
        frame = _dispatch_frame(
            "dispatch_assigned", request_id=req_id, alert=alert,
            extra={"stage": "primary", "assignee": primary},
        )
        delivered = await _send_to(primary, frame)
        logger.info("[DISPATCH] alert=%d → primary=%s (assigned) delivered=%s req=%d",
                    alert_id, primary, delivered, req_id)
        if not delivered:
            # Primary vanished between the availability check and the send.
            return await _begin_broadcast(db, req_id, alert, primary, reason="primary_unreachable")
        return {"request_id": req_id, "state": "pending_primary", "primary": primary}

    # No available primary → broadcast straight away.
    req_id = await create_dispatch_request(
        db, alert_id=alert_id, room_id=room_id,
        primary_nurse=primary, initial_state="broadcasting",
    )
    await set_alert_routed_to(db, alert_id, None, True)
    return await _begin_broadcast(
        db, req_id, alert, primary,
        reason=("no_primary" if not primary else "primary_unavailable"), fresh=True,
    )


async def _begin_broadcast(
    db: aiosqlite.Connection, req_id: int, alert: dict, primary: str | None,
    *, reason: str, fresh: bool = False,
) -> dict:
    """Broadcast a request to all FREE nurses and arm the 10s admin-escalation
    timer. `fresh` means the row was created already-broadcasting."""
    from database import set_dispatch_broadcasting, record_reroute

    if not fresh:
        row = await set_dispatch_broadcasting(db, req_id, decline_reason=reason if reason.startswith("decline") else None)
        if row is None:
            # Someone accepted / it resolved in the gap — nothing to broadcast.
            logger.info("[DISPATCH] broadcast skipped for req=%d (already settled)", req_id)
            return {"request_id": req_id, "state": "settled"}
        await record_reroute(db, alert["id"], None, True)

    frame = _dispatch_frame(
        "dispatch_request", request_id=req_id, alert=alert,
        extra={"stage": "broadcast", "assignee": None},
    )
    exclude = {primary} if primary else set()
    delivered = await _broadcast_free(db, frame, exclude=exclude)
    logger.info("[DISPATCH] req=%d broadcasting (reason=%s) → free=%s", req_id, reason, delivered)

    _arm_timer(req_id, alert["id"])

    if not delivered:
        # Nobody free at all → go straight to the admin, no point waiting 10s.
        logger.info("[DISPATCH] req=%d no free nurses → escalating to admin immediately", req_id)
        await _escalate_to_admin(db, req_id, alert, immediate=True)
    return {"request_id": req_id, "state": "broadcasting", "delivered": delivered}


async def decline(db: aiosqlite.Connection, request_id: int, nurse: str, reason: str | None) -> dict:
    """The current offeree declines. Broadcasts to all other FREE nurses.

    Only the nurse the request is currently offered to (the primary while
    pending, or the assignee) may decline; anyone else gets a no-op so a stale
    client can't derail a request that already moved on.
    """
    from database import get_dispatch_request, get_alert_by_id

    row = await get_dispatch_request(db, request_id)
    if row is None:
        return {"ok": False, "error": "not_found"}
    if row["state"] in ("resolved", "cancelled"):
        return {"ok": False, "error": "already_" + row["state"]}

    # Who is allowed to decline right now?
    offeree = row["assignee"] or row["primary_nurse"]
    if row["state"] == "pending_primary":
        offeree = row["primary_nurse"]
    if offeree != nurse:
        return {"ok": False, "error": "not_your_request"}

    alert = await get_alert_by_id(db, row["alert_id"])
    if alert is None or alert["acknowledged"]:
        await _resolve(db, request_id, row["alert_id"])
        return {"ok": False, "error": "alert_gone"}

    reason = (reason or "declined").strip()[:200]
    res = await _begin_broadcast(db, request_id, alert, row["primary_nurse"],
                                 reason="decline:" + reason)
    # Tell the decliner's own UI the card is cleared from them.
    await _send_to(nurse, {"event": "dispatch_cancelled", "request_id": request_id,
                           "alert_id": row["alert_id"], "reason": "declined"})
    return {"ok": True, **res}


async def accept(db: aiosqlite.Connection, request_id: int, nurse: str) -> dict:
    """A nurse accepts. Atomic single-winner claim.

    Guards:
      • The nurse must be AVAILABLE at click time (an offline/busy nurse whose
        stale client clicks Accept is rejected).
      • Exactly one concurrent accept wins; losers get already_taken.
    On a win the alert is routed to that nurse and all OTHER free nurses get a
    cancel frame so the card disappears from their screens.
    """
    from database import (
        get_dispatch_request, get_alert_by_id, claim_dispatch_request, record_reroute,
    )

    row = await get_dispatch_request(db, request_id)
    if row is None:
        return {"ok": False, "error": "not_found"}
    if row["state"] in ("resolved", "cancelled"):
        return {"ok": False, "error": "already_" + row["state"]}

    alert = await get_alert_by_id(db, row["alert_id"])
    if alert is None:
        await _resolve(db, request_id, row["alert_id"])
        return {"ok": False, "error": "alert_gone"}
    if alert["acknowledged"]:
        await _resolve(db, request_id, row["alert_id"])
        return {"ok": False, "error": "already_handled"}

    # A nurse may always accept a request currently assigned to HER (the primary
    # accepting her own offer). Otherwise she must be free to grab a broadcast.
    is_own = row["assignee"] == nurse
    if not is_own and not await is_available(db, nurse):
        return {"ok": False, "error": "not_available"}

    won, updated = await claim_dispatch_request(db, request_id, nurse)
    if not won:
        taker = updated["assignee"] if updated else None
        return {"ok": False, "error": "already_taken", "taken_by": taker}

    _cancel_timer(request_id)
    await record_reroute(db, row["alert_id"], nurse, False)

    # Clear the card from every other free nurse.
    cancel_frame = {"event": "dispatch_cancelled", "request_id": request_id,
                    "alert_id": row["alert_id"], "reason": "taken", "taken_by": nurse}
    import json
    from routers import ws as ws_router
    text = json.dumps(cancel_frame)
    for other in await available_nurses(db, exclude={nurse}):
        await ws_router.manager.send_to_nurse(other, text)

    # Confirm to the winner and (re)deliver the alert card as "assigned to you".
    assigned_frame = _dispatch_frame(
        "dispatch_assigned", request_id=request_id, alert=alert,
        extra={"assignee": nurse},
    )
    await _send_to(nurse, assigned_frame)
    logger.info("[DISPATCH] req=%d claimed by %s", request_id, nurse)
    return {"ok": True, "request_id": request_id, "alert_id": row["alert_id"], "assignee": nurse}


async def admin_reassign(db: aiosqlite.Connection, request_id: int, nurse: str) -> dict:
    """Admin forces a request onto a specific nurse — only if that nurse is
    AVAILABLE right now (validated server-side, not trusting the board poll)."""
    from database import get_dispatch_request, get_alert_by_id, assign_dispatch_request, record_reroute

    row = await get_dispatch_request(db, request_id)
    if row is None:
        return {"ok": False, "error": "not_found"}
    if row["state"] in ("resolved", "cancelled"):
        return {"ok": False, "error": "already_" + row["state"]}

    # Admin override: the target only needs to be ONLINE (reachable). An admin
    # can deliberately hand a request to a nurse who is already handling one —
    # that is a human decision, not something the scheduler should block.
    if not _presence_online(nurse):
        return {"ok": False, "error": "nurse_unavailable"}

    alert = await get_alert_by_id(db, row["alert_id"])
    if alert is None or alert["acknowledged"]:
        await _resolve(db, request_id, row["alert_id"])
        return {"ok": False, "error": "alert_gone"}

    updated = await assign_dispatch_request(db, request_id, nurse)
    if updated is None:
        return {"ok": False, "error": "could_not_assign"}

    _cancel_timer(request_id)
    await record_reroute(db, row["alert_id"], nurse, False)

    # Clear any lingering broadcast cards from other nurses.
    import json
    from routers import ws as ws_router
    cancel_frame = {"event": "dispatch_cancelled", "request_id": request_id,
                    "alert_id": row["alert_id"], "reason": "reassigned", "taken_by": nurse}
    text = json.dumps(cancel_frame)
    for other in await available_nurses(db, exclude={nurse}):
        await ws_router.manager.send_to_nurse(other, text)

    # Send as "dispatch_assigned" so the nurse app renders it as a card already
    # assigned to her (no Accept/Decline prompt) — an admin reassignment is a
    # forced assignment, not an open offer.
    frame = _dispatch_frame("dispatch_assigned", request_id=request_id, alert=alert,
                            extra={"stage": "admin_assigned", "assignee": nurse})
    delivered = await _send_to(nurse, frame)
    logger.info("[DISPATCH] req=%d admin-reassigned to %s delivered=%s", request_id, nurse, delivered)
    return {"ok": True, "request_id": request_id, "assignee": nurse, "delivered": delivered}


async def reclaim_from_offline_nurse(db: aiosqlite.Connection, nurse: str) -> None:
    """A nurse went offline. Any request assigned to her that is still unacked
    must not be stranded — re-broadcast it to the remaining FREE nurses.

    Guarded against races: if she comes right back online, or the alert was
    acked in the meantime, this is a no-op.
    """
    from database import get_active_dispatch_requests, get_alert_by_id

    # Give a brief grace so a quick socket flap doesn't needlessly re-broadcast.
    if OFFLINE_RECLAIM_GRACE_SECONDS > 0:
        await asyncio.sleep(OFFLINE_RECLAIM_GRACE_SECONDS)
    if _presence_online(nurse):
        return

    rows = await get_active_dispatch_requests(db)
    for row in rows:
        if row["assignee"] != nurse or row["state"] != "assigned":
            continue
        alert = await get_alert_by_id(db, row["alert_id"])
        if alert is None or alert["acknowledged"]:
            await _resolve(db, row["id"], row["alert_id"])
            continue
        logger.info("[DISPATCH] reclaiming req=%d from offline nurse %s", row["id"], nurse)
        await _begin_broadcast(db, row["id"], alert, row["primary_nurse"],
                               reason="assignee_offline")


async def on_alert_acknowledged(db: aiosqlite.Connection, alert_id: int, by_nurse: str | None) -> None:
    """Called when an alert is ACK'd — resolves its dispatch request + timer."""
    from database import get_dispatch_by_alert
    row = await get_dispatch_by_alert(db, alert_id)
    if row is None:
        return
    _cancel_timer(row["id"])
    await _resolve(db, row["id"], alert_id, by_nurse=by_nurse)


# ── Timeout + admin escalation ────────────────────────────────────────────────

def _arm_timer(request_id: int, alert_id: int) -> None:
    """Start (or restart) the 10s broadcast timer for a request."""
    _cancel_timer(request_id)
    _timers[request_id] = asyncio.create_task(_timeout_worker(request_id, alert_id))


def _arm_primary_timer(request_id: int, alert_id: int) -> None:
    """Start the primary-response timer: if the primary doesn't act in time, the
    request auto-broadcasts to free nurses."""
    _cancel_timer(request_id)
    _timers[request_id] = asyncio.create_task(_primary_timeout_worker(request_id, alert_id))


async def _primary_timeout_worker(request_id: int, alert_id: int) -> None:
    try:
        await asyncio.sleep(PRIMARY_TIMEOUT_SECONDS)
        async with aiosqlite.connect(settings.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA busy_timeout=5000")
            from database import get_dispatch_request, get_alert_by_id
            row = await get_dispatch_request(db, request_id)
            if row is None or row["state"] != "pending_primary":
                return  # primary already accepted / declined / it resolved
            alert = await get_alert_by_id(db, alert_id)
            if alert is None or alert["acknowledged"]:
                await _resolve(db, request_id, alert_id)
                return
            logger.info("[DISPATCH] req=%d primary unresponsive → broadcasting", request_id)
            await _begin_broadcast(db, request_id, alert, row["primary_nurse"],
                                   reason="primary_timeout")
    except asyncio.CancelledError:
        pass
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[DISPATCH] primary timeout worker error req=%d: %s", request_id, exc)
    finally:
        # Only drop ourselves if we're still the registered timer.
        if _timers.get(request_id) is asyncio.current_task():
            _timers.pop(request_id, None)


def _cancel_timer(request_id: int) -> None:
    task = _timers.pop(request_id, None)
    if task is not None and not task.done():
        task.cancel()


async def _timeout_worker(request_id: int, alert_id: int) -> None:
    try:
        await asyncio.sleep(BROADCAST_TIMEOUT_SECONDS)
        async with aiosqlite.connect(settings.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA busy_timeout=5000")
            from database import get_dispatch_request, get_alert_by_id
            row = await get_dispatch_request(db, request_id)
            if row is None or row["state"] != "broadcasting":
                return  # already accepted / resolved / reassigned
            alert = await get_alert_by_id(db, alert_id)
            if alert is None or alert["acknowledged"]:
                await _resolve(db, request_id, alert_id)
                return
            await _escalate_to_admin(db, request_id, alert)
    except asyncio.CancelledError:
        pass
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[DISPATCH] timeout worker error req=%d: %s", request_id, exc)
    finally:
        _timers.pop(request_id, None)


async def _escalate_to_admin(db: aiosqlite.Connection, request_id: int, alert: dict, *, immediate: bool = False) -> None:
    from database import mark_dispatch_needs_admin
    row = await mark_dispatch_needs_admin(db, request_id)
    if row is None:
        return  # a free nurse accepted in the gap — do not bother the admin
    _cancel_timer(request_id)
    frame = _dispatch_frame(
        "dispatch_needs_admin", request_id=request_id, alert=alert,
        extra={"reason": "no_free_nurse" if immediate else "timeout",
               "room_id": alert["room_id"]},
    )
    await notify_admins(frame)
    logger.info("[DISPATCH] req=%d escalated to admin (%s)",
                request_id, "no free nurse" if immediate else "10s timeout")


async def _resolve(db: aiosqlite.Connection, request_id: int, alert_id: int, *, by_nurse: str | None = None) -> None:
    from database import resolve_dispatch_for_alert
    _cancel_timer(request_id)
    await resolve_dispatch_for_alert(db, alert_id, by_nurse=by_nurse)


# ── Startup recovery ──────────────────────────────────────────────────────────

async def rearm_pending_timers() -> None:
    """On server startup, re-arm timers for requests left mid-broadcast and
    push still-pending admin escalations so nothing is stranded by a restart."""
    async with aiosqlite.connect(settings.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA busy_timeout=5000")
        from database import get_active_dispatch_requests, get_alert_by_id
        rows = await get_active_dispatch_requests(db)
        for row in rows:
            alert = await get_alert_by_id(db, row["alert_id"])
            if alert is None or alert["acknowledged"]:
                await _resolve(db, row["id"], row["alert_id"])
                continue
            if row["state"] == "broadcasting":
                _arm_timer(row["id"], row["alert_id"])
            elif row["state"] == "pending_primary":
                _arm_primary_timer(row["id"], row["alert_id"])
            elif row["state"] == "needs_admin":
                frame = _dispatch_frame(
                    "dispatch_needs_admin", request_id=row["id"], alert=alert,
                    extra={"reason": "restart_recovery", "room_id": row["room_id"]},
                )
                await notify_admins(frame)
        logger.info("[DISPATCH] re-armed %d active dispatch request(s) after startup", len(rows))


# ── Admin reset ───────────────────────────────────────────────────────────────

async def reset_nurses(db: aiosqlite.Connection) -> dict:
    """Admin action: reset ALL nurses to a clean baseline.

    - Clears every manual "occupied" flag (MANUAL_BUSY).
    - Cancels all in-flight dispatch timers.
    - Cancels all non-terminal dispatch_requests (so no nurse is left 'holding').

    After this, every ONLINE nurse reads as AVAILABLE and the dispatch board
    shows no stuck 'handling N requests'. Unacknowledged alerts themselves are
    left intact. Presence (online/offline) is driven by the live WebSocket and
    is not changed by this call.
    """
    from routing import MANUAL_BUSY
    from database import reset_all_dispatch, release_all_routed_alerts

    cleared_busy = len(MANUAL_BUSY)
    MANUAL_BUSY.clear()

    # Cancel every active timer task.
    for rid in list(_timers.keys()):
        _cancel_timer(rid)

    cancelled = await reset_all_dispatch(db)
    released = await release_all_routed_alerts(db)
    logger.info("[DISPATCH] admin reset: cleared %d manual-busy, cancelled %d dispatch request(s), released %d routed alert(s)",
                cleared_busy, cancelled, released)
    return {"cleared_manual_busy": cleared_busy, "cancelled_dispatch": cancelled, "released_alerts": released}
