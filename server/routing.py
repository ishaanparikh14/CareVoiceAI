"""
routing.py — connect the pure scheduler (scheduling.py) to the live system.

Both alert entry points (HTTP POST /audio/ingest and the patient WebSocket)
call `route_alert`. It categorises the request, finds the right nurse, and
escalates when needed:

  1. Categorise: the request's priority (Critical/Urgent/Routine) + the
     patient's acuity (ESI 1–5) give a `required_competency` (e.g. a medication
     request needs `medication_qualified`; a Critical / high-acuity request
     needs `icu_certified`) and whether it is `time_critical`.
  2. Match: among the patient's assigned nurses (priority order), pick the first
     who HAS the competency, is on the floor (not off-duty, and not on-break for
     time-critical work), and is online + not busy.
  3. Escalate on no-ack (reroute loop in main.py): primary → paired secondary →
     department supervisor → broadcast. A request is never silently dropped.

Busy = on a call (signaling registry) OR holding an unacked alert OR manually
marked busy. Live status (available / in_patient_room / on_break / off_duty)
and competencies come from the users table; see pg_database.
"""

import logging

import aiosqlite

from scheduling import (
    NurseState,
    choose_nurse,
    choose_supervisor,
)

logger = logging.getLogger(__name__)

# Manual busy toggle: usernames a nurse (or admin) marked unavailable.
MANUAL_BUSY: set[str] = set()

# Intent → competency a nurse must have to take that request.
_INTENT_COMPETENCY = {
    "Medication": "medication_qualified",
}


def set_manual_busy(nurse: str, busy: bool) -> None:
    if busy:
        MANUAL_BUSY.add(nurse)
    else:
        MANUAL_BUSY.discard(nurse)


def required_competency_for(intent: str | None, priority: str | None, acuity: int | None) -> str | None:
    """Skill a nurse MUST have for this request. High acuity / critical work
    needs an ICU-certified nurse; medication needs a medication-qualified one."""
    if (acuity is not None and acuity <= 2) or priority == "Critical" or intent == "Emergency":
        return "icu_certified"
    return _INTENT_COMPETENCY.get(intent or "")


def is_time_critical(priority: str | None, acuity: int | None) -> bool:
    return priority == "Critical" or (acuity is not None and acuity <= 2)


def nurse_is_busy(nurse: str, active_counts: dict[str, int]) -> bool:
    """Single source of truth for 'busy', shared by scheduler + admin board."""
    from routers.signal import registry as signal_registry
    on_call = signal_registry.is_busy(nurse)
    holding = active_counts.get(nurse, 0) > 0
    manual  = nurse in MANUAL_BUSY
    return on_call or holding or manual


def nurse_is_online(nurse: str) -> bool:
    """Online if present in EITHER the nurse-alert WS or the signaling registry."""
    from routers import ws as ws_router
    from routers.signal import registry as signal_registry
    return ws_router.manager.is_online(nurse) or signal_registry.is_online(nurse)


async def nurse_availability(db) -> list[dict]:
    """Per-nurse availability for the admin dispatch board, including live
    status and competencies."""
    from pg_database import get_conn, get_nurse_routing_map
    from database import get_active_alert_counts_by_nurse
    from routers.signal import registry as signal_registry

    active_counts = await get_active_alert_counts_by_nurse(db)
    async for conn in get_conn():
        routing_map = await get_nurse_routing_map(conn)
        break

    nurses: list[dict] = []
    for u, info in sorted(routing_map.items(), key=lambda kv: kv[1]["full_name"]):
        held = active_counts.get(u, 0)
        reasons = []
        if signal_registry.is_busy(u):
            reasons.append("On a call")
        if held > 0:
            reasons.append(f"Handling {held} request{'s' if held != 1 else ''}")
        if u in MANUAL_BUSY:
            reasons.append("Marked busy")
        nurses.append({
            "username": u,
            "full_name": info["full_name"],
            "ward": info["ward"],
            "online": nurse_is_online(u),
            "busy": nurse_is_busy(u, active_counts),
            "manual_busy": u in MANUAL_BUSY,
            "busy_reason": " · ".join(reasons),
            "active_alerts": held,
            "status": info["status"],
            "competencies": sorted(info["competencies"]),
            "is_supervisor": info["is_supervisor"],
        })
    return nurses


async def _gather_states(db, usernames: list[str], routing_map: dict) -> dict[str, NurseState]:
    """Build NurseState snapshots merging the DB routing map (competencies,
    status, supervisor) with live online/busy signals."""
    from database import get_active_alert_counts_by_nurse
    active_counts = await get_active_alert_counts_by_nurse(db)

    states: dict[str, NurseState] = {}
    for nurse in usernames:
        info = routing_map.get(nurse, {})
        states[nurse] = NurseState(
            username=nurse,
            online=nurse_is_online(nurse),
            busy=nurse_is_busy(nurse, active_counts),
            active_alerts=active_counts.get(nurse, 0),
            status=info.get("status", "available"),
            competencies=frozenset(info.get("competencies", set())),
            is_supervisor=info.get("is_supervisor", False),
        )
    return states


async def route_alert(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    alert_id: int,
    payload_json: str,
    exclude: set[str] | None = None,
    is_reroute: bool = False,
    allow_supervisor: bool = True,
) -> tuple[str | None, bool]:
    """Route an alert to the best assigned nurse; escalate to a supervisor; else
    broadcast. Returns (routed_to_or_None, fell_back). Side effects (WS send,
    recording routed_to/at) happen here.
    """
    from routers import ws as ws_router
    from database import set_alert_routed_to, record_reroute, get_alert_by_id
    from pg_database import (
        get_conn, get_nurses_for_room, get_nurse_routing_map,
        get_supervisor_usernames, get_patient_acuity_by_room, get_ward_for_room,
    )

    exclude = exclude or set()

    # Categorise the request from the stored alert row + patient acuity.
    row = await get_alert_by_id(db, alert_id)
    intent = row.get("intent") if row else None
    priority = row.get("priority") if row else None

    async for conn in get_conn():
        assigned_all = await get_nurses_for_room(conn, room_id)
        routing_map = await get_nurse_routing_map(conn)
        acuity = await get_patient_acuity_by_room(conn, room_id)
        ward = await get_ward_for_room(conn, room_id)
        supervisors = await get_supervisor_usernames(conn, ward)
        break

    required = required_competency_for(intent, priority, acuity)
    time_critical = is_time_critical(priority, acuity)

    assigned = [n for n in assigned_all if n not in exclude]
    # Supervisors are a separate escalation pool (exclude any already assigned
    # so a supervisor who is also the primary isn't double-counted).
    sup_pool = [s for s in supervisors if s not in exclude and s not in assigned]

    states = await _gather_states(db, list(set(assigned) | set(sup_pool)), routing_map)

    decision = choose_nurse(
        assigned, states,
        required_competency=required, time_critical=time_critical,
    )
    via = "assigned"
    if decision.chosen is None and allow_supervisor and sup_pool:
        decision = choose_supervisor(
            sup_pool, states,
            required_competency=required, time_critical=time_critical,
        )
        via = "supervisor"

    logger.info(
        "[ROUTE%s] alert=%d room=%s intent=%s prio=%s esi=%s req=%s tc=%s "
        "assigned=%s sup=%s -> %s (%s)",
        "/reroute" if is_reroute else "", alert_id, room_id, intent, priority,
        acuity, required, time_critical, assigned, sup_pool, decision.chosen, via,
    )

    record = record_reroute if is_reroute else set_alert_routed_to
    manager = ws_router.manager

    delivered = False
    if decision.chosen is not None:
        delivered = await manager.send_to_nurse(decision.chosen, payload_json)
        if not delivered:
            logger.warning("[ROUTE] delivery to %s failed — broadcasting", decision.chosen)

    if decision.chosen is None or not delivered:
        await manager.broadcast(payload_json)
        await record(db, alert_id, None, True)
        return None, True

    await record(db, alert_id, decision.chosen, False)
    return decision.chosen, False


async def redirect_alert(
    db: aiosqlite.Connection,
    *,
    alert_id: int,
    target_nurse: str,
    payload_json: str,
) -> tuple[bool, str]:
    """Dispatcher override: deliver to a SPECIFIC nurse, bypassing the scheduler.
    Falls back to broadcast if that nurse is offline."""
    from routers import ws as ws_router
    from database import record_reroute

    manager = ws_router.manager
    delivered = await manager.send_to_nurse(target_nurse, payload_json)
    if delivered:
        await record_reroute(db, alert_id, target_nurse, False)
        logger.info("[REDIRECT] alert=%d manually sent to %s", alert_id, target_nurse)
        return True, f"Alert {alert_id} redirected to {target_nurse}"

    await manager.broadcast(payload_json)
    await record_reroute(db, alert_id, None, True)
    logger.warning("[REDIRECT] target %s offline for alert=%d — broadcast", target_nurse, alert_id)
    return False, f"{target_nurse} is offline; alert {alert_id} broadcast to all nurses instead"


async def reroute_stale(db: aiosqlite.Connection, *, older_than_seconds: int) -> int:
    """Reroute unacked, already-routed alerts down the escalation chain:
    primary → secondary → … → supervisor → broadcast. We walk DOWN the assigned
    priority list by excluding everyone already given a chance (derived from
    reroute_count), so a request never ping-pongs back to a nurse who passed.
    """
    from database import get_stale_routed_unacked, get_alert_by_id
    from models import AlertResponse, WsAlertPayload
    from pg_database import get_conn, get_nurses_for_room

    stale = await get_stale_routed_unacked(db, older_than_seconds=older_than_seconds)
    rerouted = 0
    for row in stale:
        alert_id = row["id"]
        prev_nurse = row.get("routed_to")
        count = row.get("reroute_count") or 0

        # Nurses already given a chance: the first (count+1) in priority order,
        # plus whoever currently holds it. Excluding them walks strictly down.
        async for conn in get_conn():
            assigned = await get_nurses_for_room(conn, row["room_id"])
            break
        tried = set(assigned[: count + 1])
        if prev_nurse:
            tried.add(prev_nurse)

        fresh = await get_alert_by_id(db, alert_id) or row
        payload_json = WsAlertPayload.from_alert_response(
            AlertResponse.from_db_row(fresh), event="alert_updated"
        ).model_dump_json()

        chosen, fell_back = await route_alert(
            db,
            room_id=row["room_id"],
            alert_id=alert_id,
            payload_json=payload_json,
            exclude=tried,
            is_reroute=True,
            allow_supervisor=True,
        )
        logger.info(
            "[REROUTE] alert=%d prev=%s tried=%s -> %s fell_back=%s",
            alert_id, prev_nurse, sorted(tried), chosen, fell_back,
        )
        rerouted += 1
    return rerouted
