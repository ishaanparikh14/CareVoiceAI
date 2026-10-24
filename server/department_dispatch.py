# -*- coding: utf-8 -*-
"""
department_dispatch.py — delivery + acknowledge/escalate loop for non-nurse
department requests (canteen / pharmacy / housekeeping).

This is the department-side analogue of dispatch.py. The nurse dispatch engine
is deliberately left UNTOUCHED: a request that the router sends to the nurse goes
through routing.route_alert / dispatch.py exactly as before. This module only
handles the slices that go to OTHER departments, with their own lifecycle:

    pending ─ack─► acknowledged ─done─► done
       │
       └─ no ack within DEPT_ACK_SLA_SECONDS ─► escalated
                                                  └─► hand to nurse via dispatch.py
                                                      (flagged as an escalated
                                                       facility fallback so the
                                                       nurse UI shows it distinctly)

Durability / concurrency mirrors dispatch.py: authoritative state lives in
SQLite (department_requests), SLA timers are in-memory asyncio tasks keyed by
request id, and rearm_pending_department_timers() re-arms them on startup.
Single-process / single-event-loop assumption, same as dispatch.py.

Shadow mode: when settings say shadow (or a department is disabled), the router's
decision is still recorded (for later accuracy analysis) but NOTHING is actually
delivered to a department — the request stays on the nurse path. route_request()
below is only called for a REAL (non-shadow, enabled) department route; the
shadow-only path is handled by the audio ingest wiring, which just logs.
"""

from __future__ import annotations

import asyncio
import json
import logging

import aiosqlite

from config import settings

logger = logging.getLogger(__name__)

# request_id -> asyncio.Task running the SLA timer. One per request.
_timers: dict[int, asyncio.Task] = {}


def _manager():
    from routers import ws as ws_router
    return ws_router.manager


# ── Target resolution ─────────────────────────────────────────────────────────

async def _resolve_target(department: str, room_id: str, floor: str | None) -> tuple[str | None, list[str]]:
    """Resolve the delivery target + the escalation chain of usernames.

    Returns (primary_target_or_None, chain). For canteen/pharmacy the chain is
    the single department account. For housekeeping it is the floor manager, then
    the deputy, then any other housekeeping staff. A None primary means no one is
    configured — the caller escalates straight to the nurse.
    """
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


def _request_frame(event: str, req: dict, *, extra: dict | None = None) -> dict:
    frame = {
        "event": event,
        "request_id": req["id"],
        "alert_id": req["alert_id"],
        "department": req["department"],
        "room_id": req["room_id"],
        "floor": req.get("floor"),
        "transcript": req["transcript"],
        "created_at": req["created_at"],
        "repeat_count": req.get("repeat_count", 0),
    }
    if extra:
        frame.update(extra)
    return frame


# ── Nurse notice + patient feedback ───────────────────────────────────────────

async def _notify_nurse(db: aiosqlite.Connection, *, room_id: str, department: str,
                        transcript: str, alert_id: int) -> None:
    """Send the patient's primary attending nurse a passive notice that a request
    was routed to a department (informational — NOT an actionable dispatch card).
    Falls back to the next online nurse; delivery is best-effort, but the notice
    is also persisted on the alert row (dept_routed_to) so it survives reconnect."""
    from pg_database import get_conn, get_nurses_for_room

    async for conn in get_conn():
        nurses = await get_nurses_for_room(conn, room_id)
        break
    mgr = _manager()
    frame = json.dumps({
        "event": "department_notice",
        "alert_id": alert_id,
        "room_id": room_id,
        "department": department,
        "transcript": transcript,
        "message": f"Request routed to {department} for Room {room_id}",
    })
    # Prefer the primary; fall back to the first online assigned nurse.
    for nurse in nurses:
        if mgr.is_online(nurse):
            await mgr.send_to_nurse(nurse, frame)
            return
    # Nobody online right now — the audit row (dept_routed_to) carries it; also
    # drop it to admins so it's visible on the board.
    await mgr.send_to_admins(frame)


async def _notify_patient(room_id: str, message: str) -> None:
    """Push a feedback line to the patient's always-on socket (best-effort)."""
    try:
        from routers import patient_ws
        await patient_ws.notify_room(room_id, {"event": "department_update", "message": message})
    except Exception as exc:
        logger.debug("Patient feedback not delivered for room %s: %s", room_id, exc)


# ── Entry point ───────────────────────────────────────────────────────────────

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
    """Deliver one NON-NURSE department slice (REAL mode only).

    Handles dedupe, target resolution, DB row creation, WS delivery to the
    department, the nurse notice, patient feedback, and arms the SLA timer.
    Returns a small status dict.
    """
    floor = await _floor_for_room(room_id)

    # ── Dedupe: merge a repeat for the same room+department inside the window ──
    existing = await _dedupe(db, room_id=room_id, department=department)
    if existing is not None:
        from database import bump_department_repeat
        await bump_department_repeat(db, existing["id"])
        logger.info("[DEPT] deduped repeat for room=%s dept=%s → req=%d",
                    room_id, department, existing["id"])
        # Re-notify the assignee with the bumped count.
        req = await _get(db, existing["id"])
        if req and req["target_username"]:
            await _manager().send_to_dept_user(
                req["target_username"],
                json.dumps(_request_frame("department_request", req, extra={"deduped": True})),
            )
        return {"ok": True, "deduped": True, "request_id": existing["id"]}

    target, chain = await _resolve_target(department, room_id, floor)

    from database import create_department_request
    req_id = await create_department_request(
        db, alert_id=alert_id, department=department, target_username=target,
        room_id=room_id, floor=floor, transcript=transcript,
        predicted_department=predicted_department, routing_reason=routing_reason,
        confidence=confidence, model_version=model_version, shadow=False,
        initial_state="pending",
    )
    req = await _get(db, req_id)

    # No one configured/available for this department → escalate to nurse now.
    if target is None:
        logger.info("[DEPT] no target for dept=%s floor=%s → escalating req=%d to nurse",
                    department, floor, req_id)
        await _escalate_to_nurse(db, req_id, reason="no_department_target")
        return {"ok": True, "escalated": True, "request_id": req_id}

    # Deliver to the resolved department user.
    delivered = await _manager().send_to_dept_user(
        target, json.dumps(_request_frame("department_request", req))
    )
    logger.info("[DEPT] req=%d dept=%s → %s delivered=%s", req_id, department, target, delivered)

    # Nurse notice + patient feedback (best-effort; the SLA timer is the safety net).
    await _notify_nurse(db, room_id=room_id, department=department,
                        transcript=transcript, alert_id=alert_id)
    await _notify_patient(room_id, f"Your request was sent to {department}.")

    _arm_sla_timer(req_id)
    return {"ok": True, "request_id": req_id, "target": target, "delivered": delivered}


# ── Acknowledge / complete / reject ───────────────────────────────────────────

async def acknowledge(db: aiosqlite.Connection, request_id: int, by_username: str) -> dict:
    from database import acknowledge_department_request
    row = await acknowledge_department_request(db, request_id, by_username)
    if row is None:
        return {"ok": False, "error": "not_pending"}
    _cancel_timer(request_id)
    await _notify_patient(row["room_id"], f"Your {row['department']} request is being handled.")
    return {"ok": True, "request_id": request_id, "state": "acknowledged"}


async def complete(db: aiosqlite.Connection, request_id: int, by_username: str | None = None) -> dict:
    from database import complete_department_request
    row = await complete_department_request(db, request_id, by_username)
    if row is None:
        return {"ok": False, "error": "not_open"}
    _cancel_timer(request_id)
    await _notify_patient(row["room_id"], f"Your {row['department']} request is done.")
    return {"ok": True, "request_id": request_id, "state": "done"}


async def reject(db: aiosqlite.Connection, request_id: int, by_username: str, reason: str | None = None) -> dict:
    """Department bounces a request back ("not mine") → escalate to the nurse.
    The override is logged (transcript + predicted_department) for future ML labels."""
    row = await _get(db, request_id)
    if row is None:
        return {"ok": False, "error": "not_found"}
    if row["state"] not in ("pending", "acknowledged"):
        return {"ok": False, "error": "not_open"}
    logger.info("[DEPT][OVERRIDE] req=%d rejected by %s (predicted=%s reason=%s) transcript=%r",
                request_id, by_username, row["predicted_department"], reason, row["transcript"])
    await _escalate_to_nurse(db, request_id, reason=f"rejected:{reason or 'not_mine'}")
    return {"ok": True, "request_id": request_id, "state": "escalated"}


# ── SLA timer → escalate to nurse ─────────────────────────────────────────────

def _arm_sla_timer(request_id: int) -> None:
    _cancel_timer(request_id)
    _timers[request_id] = asyncio.create_task(_sla_worker(request_id))


def _cancel_timer(request_id: int) -> None:
    task = _timers.pop(request_id, None)
    if task is not None and not task.done():
        task.cancel()


async def _sla_worker(request_id: int) -> None:
    try:
        await asyncio.sleep(settings.DEPT_ACK_SLA_SECONDS)
        async with aiosqlite.connect(settings.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA busy_timeout=5000")
            row = await _get(db, request_id)
            if row is None or row["state"] != "pending":
                return  # acknowledged / done / already escalated
            logger.info("[DEPT] req=%d SLA elapsed with no ack → escalating to nurse", request_id)
            await _escalate_to_nurse(db, request_id, reason="sla_timeout")
    except asyncio.CancelledError:
        pass
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[DEPT] SLA worker error req=%d: %s", request_id, exc)
    finally:
        if _timers.get(request_id) is asyncio.current_task():
            _timers.pop(request_id, None)


async def _escalate_to_nurse(db: aiosqlite.Connection, request_id: int, *, reason: str) -> None:
    """Hand a department request to the nurse via the EXISTING dispatch engine,
    flagged so the nurse UI shows it as an escalated facility fallback."""
    from database import (
        mark_department_escalated, get_department_request, get_alert_by_id,
        mark_alert_dept_escalated,
    )
    # Move state (guarded: a late ack wins and this becomes a no-op).
    row = await mark_department_escalated(db, request_id)
    if row is None:
        row = await get_department_request(db, request_id)
        if row is None or row["state"] != "pending":
            return  # someone acknowledged first — nothing to escalate
    _cancel_timer(request_id)

    alert = await get_alert_by_id(db, row["alert_id"])
    if alert is None or alert["acknowledged"]:
        return

    # Flag the alert as an escalated department fallback for distinct nurse UI.
    await mark_alert_dept_escalated(db, row["alert_id"])
    await _notify_patient(row["room_id"], "Routing your request to the nursing station.")

    # Route through the unchanged nurse dispatch engine.
    try:
        from routing import route_alert
        await route_alert(db, room_id=row["room_id"], alert_id=row["alert_id"], payload_json="")
        logger.info("[DEPT] req=%d escalated to nurse dispatch (reason=%s)", request_id, reason)
    except Exception as exc:
        logger.warning("[DEPT] escalate-to-nurse dispatch failed for req=%d: %s", request_id, exc)


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
    """Re-arm SLA timers for pending department requests left mid-flight by a
    prior run, so a restart never strands a request. Called from main.py lifespan
    alongside dispatch.rearm_pending_timers()."""
    async with aiosqlite.connect(settings.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA busy_timeout=5000")
        from database import get_active_department_requests, get_alert_by_id
        rows = await get_active_department_requests(db)
        rearmed = 0
        for row in rows:
            alert = await get_alert_by_id(db, row["alert_id"])
            if alert is None or alert["acknowledged"]:
                continue
            _arm_sla_timer(row["id"])
            rearmed += 1
        logger.info("[DEPT] re-armed %d pending department SLA timer(s) after startup", rearmed)


# ── Feature flags (config defaults + admin runtime overrides) ─────────────────

async def resolve_flags(db: aiosqlite.Connection) -> dict:
    """Merge config.py startup defaults with admin runtime overrides persisted in
    SQLite (department_flags). Runtime wins, so a department switched off during a
    shift stays off across restarts."""
    from database import get_department_flags
    flags = {
        "enabled":      settings.DEPARTMENT_ROUTING_ENABLED,
        "shadow":       settings.DEPARTMENT_ROUTING_SHADOW,
        "canteen":      settings.DEPT_CANTEEN_ENABLED,
        "pharmacy":     settings.DEPT_PHARMACY_ENABLED,
        "housekeeping": settings.DEPT_HOUSEKEEPING_ENABLED,
    }
    overrides = await get_department_flags(db)
    flags.update({k: v for k, v in overrides.items() if k in flags})
    return flags


def _dept_enabled(flags: dict, department: str) -> bool:
    return bool(flags.get(department, True))


# ── Top-level alert routing hook ──────────────────────────────────────────────

async def process_alert_routing(
    db: aiosqlite.Connection,
    *,
    alert_id: int,
    room_id: str,
    transcript: str,
    intent,                      # models.Intent
    stt_avg_logprob: float | None = None,
    stt_corrected: bool = False,
) -> dict:
    """Run the department router for a freshly created alert and act on it.

    Returns a dict describing what happened:
      {"nurse_primary": bool, "departments": [..], "shadow": bool, "reason": str}

    Contract with the caller (audio.py / patient_ws.py):
      • If this returns nurse_primary=True, the caller MUST still run the normal
        nurse dispatch (route_alert) for this alert — department routing never
        removes the nurse from a request that needs her.
      • If nurse_primary=False, the request was fully handled by departments and
        the caller should NOT run nurse dispatch.
      • In SHADOW mode (or when routing is disabled) this ALWAYS returns
        nurse_primary=True and dispatches nothing — the decision is only logged
        on the alert row for later accuracy analysis.
    """
    from department_router import route_departments, Department, MODEL_VERSION
    from database import set_alert_department_routing

    flags = await resolve_flags(db)

    # Resolve the diet restriction (gates Food/Water) from Postgres.
    diet = None
    try:
        from pg_database import get_diet_restriction_for_room
        diet = await get_diet_restriction_for_room(room_id)
    except Exception:
        diet = None

    decisions = route_departments(
        transcript, intent,
        diet_restriction=diet,
        stt_avg_logprob=stt_avg_logprob,
        stt_corrected=stt_corrected,
        min_confidence=settings.DEPT_MIN_CONFIDENCE,
        min_stt_avg_logprob=settings.DEPT_MIN_STT_AVG_LOGPROB,
    )

    nurse_primary = any(d.department == Department.NURSE and d.is_primary for d in decisions)
    non_nurse = [d for d in decisions if d.department != Department.NURSE]
    dept_names = [d.department.value for d in non_nurse]
    primary = next((d for d in decisions if d.is_primary), decisions[0])

    routing_disabled = not flags["enabled"]
    shadow = flags["shadow"] or routing_disabled

    # ── Always write the audit row (this is the shadow-mode accuracy log too) ─
    await set_alert_department_routing(
        db, alert_id,
        dept_routed_to=(",".join(dept_names) if dept_names else "nurse"),
        confidence=primary.confidence,
        reason=f"{primary.routing_reason}: {primary.reason}",
        model_version=MODEL_VERSION,
        shadow=shadow,
    )

    # ── Shadow / disabled: log the decision, record shadow rows, change nothing ─
    if shadow:
        for d in non_nurse:
            # A shadow department_requests row (not delivered) for later analysis.
            try:
                from database import create_department_request
                await create_department_request(
                    db, alert_id=alert_id, department=d.department.value, target_username=None,
                    room_id=room_id, floor=await _floor_for_room(room_id), transcript=transcript,
                    predicted_department=d.department.value, routing_reason=d.routing_reason,
                    confidence=d.confidence, model_version=d.model_version, shadow=True,
                    initial_state="pending",
                )
            except Exception as exc:
                logger.debug("shadow dept row insert failed: %s", exc)
        logger.info("[DEPT][SHADOW] alert=%d would route %s (primary=%s) — nurse path unchanged",
                    alert_id, dept_names or ["nurse"], primary.department.value)
        return {"nurse_primary": True, "departments": dept_names, "shadow": True,
                "reason": primary.reason}

    # ── Live mode: dispatch each enabled non-nurse slice ──────────────────────
    dispatched: list[str] = []
    for d in non_nurse:
        if not _dept_enabled(flags, d.department.value):
            # Department toggled off → fold this slice back to the nurse.
            nurse_primary = True
            logger.info("[DEPT] dept=%s disabled → folding alert=%d back to nurse",
                        d.department.value, alert_id)
            continue
        res = await route_request(
            db, alert_id=alert_id, department=d.department.value, room_id=room_id,
            transcript=transcript, predicted_department=d.department.value,
            routing_reason=d.routing_reason, confidence=d.confidence,
            model_version=d.model_version,
        )
        if res.get("escalated"):
            nurse_primary = True   # no target → nurse owns it after all
        else:
            dispatched.append(d.department.value)

    return {"nurse_primary": nurse_primary, "departments": dispatched, "shadow": False,
            "reason": primary.reason}
