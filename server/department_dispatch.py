# -*- coding: utf-8 -*-
"""
department_dispatch.py — delivery + lifecycle for non-nurse work (v3).

Routing model (slides): nurse-bound intents never reach this module. Triage
(triage.py) runs BEFORE the alert is created and sends here only:

  * facility / ground-support requests  -> the floor manager of the room's floor
  * food requests spoken by the patient -> stay with the nurse as a flagged food
    request (patients cannot order food); attenders and nurses place the real
    canteen order through the order endpoints below.

Housekeeping (ground force) never goes to the nurse — no "department_notice",
no nurse SLA escalation. An unacknowledged request moves along the housekeeping
chain on T_esc (floor manager -> deputy -> other housekeeping staff); after the
last person it is marked 'unattended' and the admin is told. The nurse is used
only when the router cannot place the request at all and the workflow setting
'housekeeping_nurse_fallback' is on.

Food orders (attender / nurse) enforce the patient's diet on the server and the
dietician's recommendation when 'dietician_augments' is on. Nurse approval is a
workflow toggle (off by default). Pharmacy: patients can never order medicine;
a nurse-only medicine order exists behind 'pharmacy_orders_nurse_only' (off).

Single-process assumption (same as dispatch.py): timers are in-memory asyncio
tasks keyed by request id; state lives in SQLite and timers are re-armed on start.
"""

from __future__ import annotations

import asyncio
import json
import logging

import aiosqlite

from config import settings

logger = logging.getLogger(__name__)

# request_id -> asyncio.Task (chain escalation or auto-deliver). One per request.
_timers: dict[int, asyncio.Task] = {}


def _manager():
    from routers import ws as ws_router
    return ws_router.manager


async def _cfg(db) -> dict:
    from workflow_config import get_config
    return await get_config(db)


# ── Target resolution ─────────────────────────────────────────────────────────

async def _resolve_target(department: str, room_id: str, floor: str | None) -> tuple[str | None, list[str]]:
    """Return (first target, ordered chain). Housekeeping: floor manager, deputy,
    then the other housekeeping staff. Other departments: the department account."""
    from pg_database import get_conn, get_department_user, get_floor_manager_chain
    async for conn in get_conn():
        if department == "housekeeping":
            chain = await get_floor_manager_chain(conn, floor)
            usernames = [c["username"] for c in chain]
            return (usernames[0] if usernames else None), usernames
        row = await get_department_user(conn, department)
        if row is None:
            return None, []
        return row["username"], [row["username"]]
    return None, []


def _first_online(chain: list[str], start: int) -> int:
    """Index of the first chain member from `start` who is logged in. If none
    is online, `start` itself (the request waits for that person)."""
    try:
        mgr = _manager()
        for i in range(max(start, 0), len(chain)):
            if mgr.is_dept_user_online(chain[i]):
                return i
    except Exception:
        pass
    return start


_STOP = {"the", "a", "an", "my", "is", "are", "in", "on", "of", "to", "and", "please", "i", "me",
         "it", "this", "that", "room", "has", "have", "can", "you", "someone", "need", "want",
         "not", "working", "there", "here", "some", "any"}


def _content_words(text: str) -> set[str]:
    import re
    return {w for w in re.findall(r"[a-z\u0900-\u097f]+", (text or "").lower())
            if len(w) > 2 and w not in _STOP}


def same_request(a: str, b: str) -> bool:
    """True when two housekeeping transcripts are about the same thing (a repeat).
    Conservative: unclear → treated as different, so nothing is lost."""
    wa, wb = _content_words(a), _content_words(b)
    if not wa or not wb:
        return (a or "").strip().lower() == (b or "").strip().lower()
    overlap = len(wa & wb) / min(len(wa), len(wb))
    return overlap >= 0.5


def _request_frame(event: str, req: dict, *, extra: dict | None = None) -> dict:
    frame = {
        "event": event,
        "request_id": req["id"],
        "alert_id": req["alert_id"],
        "department": req["department"],
        "kind": req.get("kind", "facility"),
        "room_id": req["room_id"],
        "floor": req.get("floor"),
        "transcript": req["transcript"],
        "created_at": req["created_at"],
        "state": req.get("state", "pending"),
        "order_status": req.get("order_status"),
        "item_name": req.get("item_name"),
        "item_detail": req.get("item_detail"),
        "item_notes": req.get("item_notes"),
        "diet_restriction": req.get("diet_restriction"),
        "ordered_by": req.get("ordered_by"),
        "ordered_by_role": req.get("ordered_by_role"),
        "repeat_count": req.get("repeat_count", 0),
    }
    if extra:
        frame.update(extra)
    return frame


async def _event(db, request_id: int, event: str, *, actor=None, target=None, detail=None) -> None:
    from database import utcnow
    try:
        await db.execute(
            "INSERT INTO dept_events (request_id, event, actor, target, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (request_id, event, actor, target, detail, utcnow()),
        )
        await db.commit()
    except Exception as exc:  # pragma: no cover - audit must never break delivery
        logger.warning("[DEPT] could not write dept_event: %s", exc)


async def _notify_patient(room_id: str, message: str) -> None:
    """Push a feedback line to the patient's always-on socket (best-effort)."""
    try:
        from routers import patient_ws
        await patient_ws.notify_room(room_id, {"event": "department_update", "message": message})
    except Exception as exc:
        logger.debug("Patient feedback not delivered for room %s: %s", room_id, exc)


# ── Facility / housekeeping requests ──────────────────────────────────────────

async def route_request(
    db: aiosqlite.Connection,
    *,
    alert_id: int,
    department: str,
    room_id: str,
    transcript: str,
    predicted_department: str,
    routing_reason: str,
    confidence: float,
    model_version: str,
) -> dict:
    """Deliver a ground-support request to the housekeeping chain.

    Returns {"ok", "request_id", "target"} or {"ok", "deduped"} or
    {"ok": False, "unplaced": True} when nobody can take it (the caller then
    applies the nurse-fallback workflow setting)."""
    floor = await _floor_for_room(room_id)

    # Merge only a REPEAT of the same request (patient says it twice). A
    # different problem from the same room ("TV remote" after "spill") is a new
    # request that starts again at the floor manager — it must not be folded
    # into an older one that was passed on with "Not mine".
    existing = await _dedupe(db, room_id=room_id, department=department)
    if existing is not None and not same_request(existing.get("transcript") or "", transcript):
        existing = None
    if existing is not None:
        from database import bump_department_repeat
        await bump_department_repeat(db, existing["id"])
        req = await _get(db, existing["id"])
        if req and req["target_username"]:
            await _manager().send_to_dept_user(
                req["target_username"],
                json.dumps(_request_frame("department_request", req, extra={"deduped": True})),
            )
        logger.info("[DEPT] deduped repeat room=%s dept=%s -> req=%d", room_id, department, existing["id"])
        return {"ok": True, "deduped": True, "request_id": existing["id"]}

    target, chain = await _resolve_target(department, room_id, floor)
    if target is None:
        logger.info("[DEPT] no %s staff for floor=%s — request cannot be placed", department, floor)
        return {"ok": False, "unplaced": True}
    # Start with the first person in the chain who is logged in (floor manager,
    # else deputy, else other housekeeping). Nobody online: the floor manager
    # still gets it (delivered when they log in) and the T_esc timer runs.
    start_idx = _first_online(chain, 0)
    target = chain[start_idx] if chain else target

    from database import create_department_request
    req_id = await create_department_request(
        db, alert_id=alert_id, department=department, target_username=target,
        room_id=room_id, floor=floor, transcript=transcript,
        predicted_department=predicted_department, routing_reason=routing_reason,
        confidence=confidence, model_version=model_version, shadow=False,
        initial_state="pending",
    )
    await db.execute(
        "UPDATE department_requests SET chain = ?, chain_idx = ?, kind = 'facility' WHERE id = ?",
        (json.dumps(chain), start_idx, req_id),
    )
    await db.commit()
    await _event(db, req_id, "assigned", target=target, detail=f"chain={chain}")

    req = await _get(db, req_id)
    delivered = await _manager().send_to_dept_user(target, json.dumps(_request_frame("department_request", req)))
    logger.info("[DEPT] req=%d %s -> %s delivered=%s", req_id, department, target, delivered)
    await _notify_patient(room_id, "Your request was sent to housekeeping.")
    _arm_chain_timer(req_id, await _t_esc(db))
    return {"ok": True, "request_id": req_id, "target": target, "delivered": delivered}


async def _t_esc(db) -> int:
    try:
        return int((await _cfg(db)).get("t_esc_seconds", settings.DEPT_ACK_SLA_SECONDS))
    except Exception:
        return settings.DEPT_ACK_SLA_SECONDS


async def acknowledge(db: aiosqlite.Connection, request_id: int, by_username: str) -> dict:
    from database import acknowledge_department_request
    row = await acknowledge_department_request(db, request_id, by_username)
    if row is None:
        return {"ok": False, "error": "not_pending"}
    _cancel_timer(request_id)
    await _event(db, request_id, "acknowledged", actor=by_username)
    await _notify_patient(row["room_id"], "Your request is being handled.")
    return {"ok": True, "request_id": request_id, "state": "acknowledged"}


async def complete(db: aiosqlite.Connection, request_id: int, by_username: str | None = None) -> dict:
    from database import complete_department_request
    row = await complete_department_request(db, request_id, by_username)
    if row is None:
        return {"ok": False, "error": "not_open"}
    _cancel_timer(request_id)
    await _event(db, request_id, "done", actor=by_username)
    await _notify_patient(row["room_id"], "Your request is done.")
    return {"ok": True, "request_id": request_id, "state": "done"}


async def reject(db: aiosqlite.Connection, request_id: int, by_username: str, reason: str | None = None) -> dict:
    """'Not mine': pass the request to the next person in the housekeeping chain.
    Logged as an override (accuracy signal for the routing stats)."""
    row = await _get(db, request_id)
    if row is None:
        return {"ok": False, "error": "not_found"}
    if row["state"] not in ("pending", "acknowledged"):
        return {"ok": False, "error": "not_open"}
    await _event(db, request_id, "override", actor=by_username, detail=reason or "not_mine")
    logger.info("[DEPT][OVERRIDE] req=%d by %s (%s) transcript=%r",
                request_id, by_username, reason, row["transcript"])
    nxt = await _advance_chain(db, request_id, reason="not_mine")
    return {"ok": True, "request_id": request_id, "state": "reassigned" if nxt else "unattended",
            "next": nxt}


async def _advance_chain(db, request_id: int, *, reason: str) -> str | None:
    """Move a request to the next person in its chain. Returns the new target, or
    None once the chain is exhausted (request becomes 'unattended', admin told).
    Never sends anything to the nurse."""
    row = await _get(db, request_id)
    if row is None or row["state"] in ("done", "cancelled"):
        return None
    try:
        chain = json.loads(row.get("chain") or "[]")
    except Exception:
        chain = []
    # Next person who is logged in; if nobody further is online, the next in line.
    idx = _first_online(chain, int(row.get("chain_idx") or 0) + 1)
    from database import utcnow
    if idx < len(chain):
        nxt = chain[idx]
        await db.execute(
            "UPDATE department_requests SET target_username = ?, chain_idx = ?, state = 'pending', "
            "updated_at = ? WHERE id = ?",
            (nxt, idx, utcnow(), request_id),
        )
        await db.commit()
        await _event(db, request_id, "escalated", target=nxt, detail=reason)
        req = await _get(db, request_id)
        await _manager().send_to_dept_user(nxt, json.dumps(_request_frame("department_request", req,
                                                                          extra={"escalated": True})))
        prev = row.get("target_username")
        if prev and prev != nxt:
            await _manager().send_to_dept_user(prev, json.dumps({"event": "department_cancelled",
                                                                 "request_id": request_id}))
        logger.info("[DEPT] req=%d moved to %s (%s)", request_id, nxt, reason)
        _arm_chain_timer(request_id, await _t_esc(db))
        return nxt

    await db.execute(
        "UPDATE department_requests SET state = 'escalated', escalated_at = ?, updated_at = ? WHERE id = ?",
        (utcnow(), utcnow(), request_id),
    )
    await db.commit()
    await _event(db, request_id, "unattended", detail=reason)
    await _manager().send_to_admins(json.dumps({
        "event": "department_unattended", "request_id": request_id,
        "room_id": row["room_id"], "department": row["department"],
        "transcript": row["transcript"],
    }))
    logger.warning("[DEPT] req=%d unattended after the whole housekeeping chain", request_id)
    return None


def _arm_chain_timer(request_id: int, seconds: int) -> None:
    _cancel_timer(request_id)
    _timers[request_id] = asyncio.create_task(_chain_worker(request_id, seconds))


def _cancel_timer(request_id: int) -> None:
    task = _timers.pop(request_id, None)
    if task is not None and not task.done():
        task.cancel()


async def _chain_worker(request_id: int, seconds: int) -> None:
    try:
        await asyncio.sleep(seconds)
        async with aiosqlite.connect(settings.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA busy_timeout=5000")
            row = await _get(db, request_id)
            if row is None or row["state"] != "pending":
                return
            if _timers.get(request_id) is asyncio.current_task():
                _timers.pop(request_id, None)
            await _advance_chain(db, request_id, reason="t_esc_timeout")
    except asyncio.CancelledError:
        pass
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[DEPT] chain worker error req=%d: %s", request_id, exc)
    finally:
        if _timers.get(request_id) is asyncio.current_task():
            _timers.pop(request_id, None)


# ── Small helpers ─────────────────────────────────────────────────────────────

async def _get(db: aiosqlite.Connection, request_id: int) -> dict | None:
    from database import get_department_request
    return await get_department_request(db, request_id)


async def _dedupe(db: aiosqlite.Connection, *, room_id: str, department: str) -> dict | None:
    from database import find_dedupe_department_request
    return await find_dedupe_department_request(
        db, room_id=room_id, department=department,
        within_seconds=settings.DEPT_DEDUPE_WINDOW_SECONDS,
    )


async def _floor_for_room(room_id: str) -> str | None:
    from pg_database import get_floor_for_room
    return await get_floor_for_room(room_id)


# ── Startup recovery ──────────────────────────────────────────────────────────

async def rearm_pending_department_timers() -> None:
    """Re-arm chain timers for pending housekeeping requests and auto-deliver
    timers for orders being prepared, so a restart never strands work."""
    async with aiosqlite.connect(settings.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA busy_timeout=5000")
        from database import get_active_department_requests
        rows = await get_active_department_requests(db)
        t = await _t_esc(db)
        rearmed = 0
        for row in rows:
            if (row.get("kind") or "facility") == "facility":
                _arm_chain_timer(row["id"], t)
                rearmed += 1
        async with db.execute(
            "SELECT id FROM department_requests WHERE order_status = 'preparing' AND state != 'done'"
        ) as cur:
            for r in await cur.fetchall():
                _arm_deliver_timer(r["id"])
                rearmed += 1
        logger.info("[DEPT] re-armed %d department timer(s) after startup", rearmed)


# ── Legacy flag view (kept for /admin/department/config compatibility) ───────

async def resolve_flags(db: aiosqlite.Connection) -> dict:
    cfg = await _cfg(db)
    return {
        "enabled": True,
        "shadow": cfg["shadow"],
        "canteen": cfg["canteen_routing"],
        "pharmacy": cfg["pharmacy_orders_nurse_only"],
        "housekeeping": cfg["housekeeping_routing"],
    }


# ── Top-level hook: called right after an alert is created ────────────────────

async def process_alert_routing(
    db: aiosqlite.Connection,
    *,
    alert_id: int,
    room_id: str,
    transcript: str,
    intent=None,
    request_class: str = "nurse",
    confidence: float = 1.0,
    reason: str = "",
    model_version: str = "kw-router-v2",
    stt_avg_logprob: float | None = None,
    stt_corrected: bool = False,
) -> dict:
    """Act on the triage decision for a freshly created alert.

    Returns {"nurse_primary": bool, "departments": [...], "shadow": bool, "reason"}.
    nurse_primary=True means the caller must run the normal nurse dispatch.

      nurse    -> nurse dispatch (router skipped)
      food     -> nurse dispatch; the nurse/attender places the canteen order
      facility -> housekeeping chain; nurse only if it cannot be placed AND the
                  'housekeeping_nurse_fallback' setting is on
    """
    from database import set_alert_department_routing
    cfg = await _cfg(db)
    shadow = bool(cfg.get("shadow"))

    dept = {"facility": "housekeeping", "food": "canteen"}.get(request_class)
    await set_alert_department_routing(
        db, alert_id,
        dept_routed_to=(dept or "nurse"),
        confidence=confidence,
        reason=f"{request_class}: {reason}"[:300],
        model_version=model_version,
        shadow=shadow,
    )

    if request_class == "nurse":
        return {"nurse_primary": True, "departments": [], "shadow": shadow, "reason": reason}

    if request_class == "food":
        # Patients cannot order food themselves: the nurse sees a Routine food
        # request and the attender/nurse places the order with the diet applied.
        return {"nurse_primary": True, "departments": [], "shadow": shadow,
                "reason": "patient food request — nurse/attender places the order"}

    # Facility / ground support.
    if shadow or not cfg.get("housekeeping_routing", True):
        why = "shadow mode" if shadow else "housekeeping routing off"
        await _set_visible(db, alert_id, True)
        return {"nurse_primary": True, "departments": [], "shadow": shadow, "reason": why}

    res = await route_request(
        db, alert_id=alert_id, department="housekeeping", room_id=room_id,
        transcript=transcript, predicted_department="housekeeping",
        routing_reason=model_version, confidence=confidence, model_version=model_version,
    )
    if res.get("ok"):
        await _set_visible(db, alert_id, False)
        return {"nurse_primary": False, "departments": ["housekeeping"], "shadow": False,
                "reason": reason}

    # Router could not place it at all.
    if cfg.get("housekeeping_nurse_fallback", True):
        await _set_visible(db, alert_id, True)
        return {"nurse_primary": True, "departments": [], "shadow": False,
                "reason": "no housekeeping staff for this floor — nurse fallback"}
    await _set_visible(db, alert_id, False)
    await _manager().send_to_admins(json.dumps({
        "event": "department_unattended", "alert_id": alert_id, "room_id": room_id,
        "department": "housekeeping", "transcript": transcript,
    }))
    return {"nurse_primary": False, "departments": [], "shadow": False,
            "reason": "unplaced; nurse fallback off — admin notified"}


async def _set_visible(db, alert_id: int, visible: bool) -> None:
    await db.execute("UPDATE alerts SET visible_to_nurse = ? WHERE id = ?", (1 if visible else 0, alert_id))
    await db.commit()


# ══════════════════════════════════════════════════════════════════════════════
#  FOOD ORDERS (attender / nurse)   and   NURSE-ONLY MEDICINE ORDERS
#  requested -> pending_approval -> approved -> preparing -> delivered (food)
#  medicine: approved -> delivered
# ══════════════════════════════════════════════════════════════════════════════

class OrderError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


async def _push_dept(username: str | None, req: dict, event: str = "order_update") -> None:
    if username:
        await _manager().send_to_dept_user(username, json.dumps(_request_frame(event, req)))


async def _notify_nurse_order(db, req: dict, event: str = "order_needs_approval") -> None:
    from pg_database import get_conn, get_nurses_for_room
    async for conn in get_conn():
        nurses = await get_nurses_for_room(conn, req["room_id"])
        break
    frame = json.dumps(_request_frame(event, req))
    mgr = _manager()
    sent = False
    for nurse in nurses:
        if mgr.is_online(nurse):
            await mgr.send_to_nurse(nurse, frame)
            sent = True
    if not sent:
        await mgr.send_to_admins(frame)


async def place_food_order(
    db, *, room_id: str, item_id: int, by: str, by_role: str, notes: str | None = None,
) -> dict:
    """Place a canteen order for a patient. Called by an attender (from the
    patient dashboard) or by a nurse. Enforces workflow + diet on the server."""
    cfg = await _cfg(db)
    if not cfg.get("canteen_routing", True):
        raise OrderError("workflow_disabled", "Canteen ordering is switched off by the hospital.", 403)
    if by_role == "attender" and not cfg.get("attender_can_order", True):
        raise OrderError("workflow_disabled", "Attender ordering is switched off by the hospital.", 403)

    import diet_rules
    from pg_database import get_menu_item, get_diet_restriction_for_room, get_dietician_plan
    item = await get_menu_item(item_id)
    if item is None:
        raise OrderError("not_found", "Menu item not found.", 404)

    # An attender orders a meal for HIMSELF (billed to the room), never for the
    # patient — so the patient's diet does not apply and no nurse approval is
    # needed. Food for the patient is ordered by the nurse, diet enforced.
    for_attender = by_role == "attender"
    plan = None
    if for_attender:
        diet = None
    else:
        diet = (await get_diet_restriction_for_room(room_id)) or "none"
        plan = await get_dietician_plan(room_id) if cfg.get("dietician_augments", True) else None
        verdict = diet_rules.check_item(item, diet, plan)
        if not verdict["allowed"]:
            raise OrderError(verdict["code"], verdict["message"],
                             409 if verdict["code"] == "clearance_required" else 422)

    needs_approval = bool(cfg.get("nurse_approval_food", False)) and by_role not in ("nurse", "attender")
    status = "pending_approval" if needs_approval else "preparing"
    floor = await _floor_for_room(room_id)
    target, _ = await _resolve_target("canteen", room_id, floor)

    from database import create_order_request, insert_alert
    from pg_database import get_patient_name_by_room
    patient_name = await get_patient_name_by_room(room_id)
    alert_id = await insert_alert(
        db, room_id=room_id, priority="Routine", intent="Food/Water", distress_score=0.0,
        transcript=f"[ORDER] {item['name']} ordered by {by_role} {by}",
        patient_name=patient_name,
        summary=f"attender meal: {item['name']}" if for_attender else f"food order: {item['name']}",
        request_class="food", model_intent=None, visible_to_nurse=False,
    )
    await db.execute("UPDATE alerts SET acknowledged = 1, ack_by = ? WHERE id = ?", (by, alert_id))
    await db.commit()

    dietician_note = (plan or {}).get("notes") if plan else None
    item_notes = "; ".join(x for x in [
        "ATTENDER'S OWN MEAL — not for the patient" if for_attender else None,
        notes, f"dietician: {dietician_note}" if dietician_note else None] if x) or None
    who_for = f"the attender of Room {room_id}" if for_attender else f"Room {room_id}"
    req_id = await create_order_request(
        db, alert_id=alert_id, department="canteen", kind="food", target_username=target,
        room_id=room_id, floor=floor, transcript=f"{item['name']} for {who_for}",
        predicted_department="canteen", routing_reason=f"order:{by_role}",
        confidence=1.0, model_version="order", needs_approval=needs_approval,
        order_status=status, item_name=item["name"], item_detail=item.get("description"),
        diet_restriction=diet,
    )
    await db.execute(
        "UPDATE department_requests SET ordered_by = ?, ordered_by_role = ?, item_notes = ? WHERE id = ?",
        (by, by_role, item_notes, req_id),
    )
    await db.commit()
    req = await _get(db, req_id)
    if needs_approval:
        await _notify_nurse_order(db, req)
        await _notify_patient(room_id, f"{item['name']} ordered — waiting for nurse approval.")
    else:
        await _push_dept(target, req, "order_new")
        if not for_attender:
            await _notify_patient(room_id, f"{item['name']} ordered — the kitchen is preparing it.")
        _arm_deliver_timer(req_id)
    logger.info("[ORDER] food req=%d room=%s item=%s by %s(%s) status=%s",
                req_id, room_id, item["name"], by, by_role, status)
    return {"ok": True, "request_id": req_id, "order_status": status}


async def place_medicine_order(db, *, room_id: str, name: str, dosage: str, by: str) -> dict:
    """Nurse-only medicine order to the pharmacy (behind a workflow toggle)."""
    cfg = await _cfg(db)
    if not cfg.get("pharmacy_orders_nurse_only", False):
        raise OrderError("workflow_disabled", "Pharmacy orders are switched off by the hospital.", 403)
    floor = await _floor_for_room(room_id)
    target, _ = await _resolve_target("pharmacy", room_id, floor)
    from database import create_order_request, insert_alert
    alert_id = await insert_alert(
        db, room_id=room_id, priority="Routine", intent="Medication", distress_score=0.0,
        transcript=f"[ORDER] {name} {dosage} ordered by nurse {by}",
        summary=f"medicine order: {name}", request_class="nurse", visible_to_nurse=False,
    )
    await db.execute("UPDATE alerts SET acknowledged = 1, ack_by = ? WHERE id = ?", (by, alert_id))
    await db.commit()
    req_id = await create_order_request(
        db, alert_id=alert_id, department="pharmacy", kind="medicine", target_username=target,
        room_id=room_id, floor=floor, transcript=f"{name} for Room {room_id}",
        predicted_department="pharmacy", routing_reason="order:nurse", confidence=1.0,
        model_version="order", needs_approval=False, order_status="approved",
        item_name=name, item_detail=dosage,
    )
    await db.execute("UPDATE department_requests SET ordered_by = ?, ordered_by_role = 'nurse' WHERE id = ?",
                     (by, req_id))
    await db.commit()
    await _push_dept(target, await _get(db, req_id), "order_new")
    return {"ok": True, "request_id": req_id, "order_status": "approved"}


async def order_approve(db, request_id: int, by: str):
    from database import set_order_status
    req = await set_order_status(db, request_id, "approved", by=by)
    if req is None:
        return {"ok": False, "error": "not_found"}
    if req["kind"] == "food":
        req = await set_order_status(db, request_id, "preparing", by=by)
        await _push_dept(req["target_username"], req, "order_new")
        await _notify_patient(req["room_id"], "Approved — the kitchen is preparing your meal.")
        _arm_deliver_timer(request_id)
        return {"ok": True, "order_status": "preparing"}
    await _push_dept(req["target_username"], req, "order_update")
    return {"ok": True, "order_status": "approved"}


async def order_reject(db, request_id: int, by: str, reason: str | None = None):
    from database import set_order_status
    req = await set_order_status(db, request_id, "rejected", by=by)
    if req is None:
        return {"ok": False, "error": "not_found"}
    _cancel_timer(request_id)
    await _push_dept(req["target_username"], req, "order_update")
    await _notify_patient(req["room_id"], "Your order was not approved. A nurse will follow up.")
    logger.info("[ORDER] req=%d rejected by %s (%s)", request_id, by, reason)
    return {"ok": True, "order_status": "rejected"}


async def order_mark_preparing(db, request_id: int, by: str):
    from database import set_order_status
    req = await set_order_status(db, request_id, "preparing", by=by)
    if req is None:
        return {"ok": False, "error": "not_found"}
    await _notify_patient(req["room_id"], "Your order is being prepared.")
    _arm_deliver_timer(request_id)
    return {"ok": True, "order_status": "preparing"}


async def order_mark_delivered(db, request_id: int, by: str):
    from database import set_order_status
    _cancel_timer(request_id)
    req = await set_order_status(db, request_id, "delivered", by=by)
    if req is None:
        return {"ok": False, "error": "not_found"}
    await _push_dept(req["target_username"], req, "order_update")
    await _notify_patient(req["room_id"], "Your order has been delivered.")
    return {"ok": True, "order_status": "delivered"}


def _arm_deliver_timer(request_id: int) -> None:
    secs = settings.DEPT_PREPARING_AUTO_DELIVER_SECONDS
    if secs and secs > 0:
        _cancel_timer(request_id)
        _timers[request_id] = asyncio.create_task(_deliver_worker(request_id, secs))


async def _deliver_worker(request_id: int, secs: int) -> None:
    try:
        await asyncio.sleep(secs)
        async with aiosqlite.connect(settings.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA busy_timeout=5000")
            req = await _get(db, request_id)
            if req is None or req.get("order_status") != "preparing":
                return
            if _timers.get(request_id) is asyncio.current_task():
                _timers.pop(request_id, None)
            await order_mark_delivered(db, request_id, by="auto")
    except asyncio.CancelledError:
        pass
    except Exception as exc:  # pragma: no cover
        logger.warning("[ORDER] auto-deliver error req=%d: %s", request_id, exc)
    finally:
        if _timers.get(request_id) is asyncio.current_task():
            _timers.pop(request_id, None)
