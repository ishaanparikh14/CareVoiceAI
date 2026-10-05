"""
routing.py — connect the pure scheduler (scheduling.py) to the live system.

Both alert entry points (HTTP POST /audio/ingest and the patient WebSocket)
call `route_alert` instead of broadcasting to all nurses. This gathers the
current state — who is assigned to the room, who is online, who is busy, and
how many unacked alerts each holds — asks the scheduler for the next available
nurse, delivers to that nurse, and falls back to a full broadcast when nobody
assigned is free (so an alert is never silently dropped).

Busy definition (agreed)
------------------------
A nurse is BUSY if ANY of:
  • they are on an active WebRTC call (signaling registry `is_busy`), or
  • they are holding an unacknowledged alert already routed to them, or
  • they have set a manual busy flag (MANUAL_BUSY set below).
"""

import logging

import aiosqlite

from scheduling import NurseState, choose_nurse

logger = logging.getLogger(__name__)

# Manual busy toggle: usernames a nurse (or admin) marked unavailable.
# In-memory, single-process — mirrors the other registries in this app.
MANUAL_BUSY: set[str] = set()


def set_manual_busy(nurse: str, busy: bool) -> None:
    if busy:
        MANUAL_BUSY.add(nurse)
    else:
        MANUAL_BUSY.discard(nurse)


def nurse_is_busy(nurse: str, active_counts: dict[str, int]) -> bool:
    """Single source of truth for 'busy': on a call, holding an unacked alert,
    or manually marked unavailable. Used by BOTH the scheduler and the admin
    dispatch board so free/occupied always agree."""
    from routers.signal import registry as signal_registry
    on_call = signal_registry.is_busy(nurse)
    holding = active_counts.get(nurse, 0) > 0
    manual  = nurse in MANUAL_BUSY
    return on_call or holding or manual


def nurse_is_online(nurse: str) -> bool:
    """Online if present in EITHER the nurse-alert WS manager or the signaling
    registry (a nurse may hold one socket but not the other)."""
    from routers import ws as ws_router
    from routers.signal import registry as signal_registry
    return ws_router.manager.is_online(nurse) or signal_registry.is_online(nurse)


async def nurse_availability(db) -> list[dict]:
    """Return availability for every approved nurse, for the admin dispatch
    board. Shape: [{username, full_name, online, busy, active_alerts}]."""
    from pg_database import get_conn, get_approved_nurse_usernames
    from database import get_active_alert_counts_by_nurse

    active_counts = await get_active_alert_counts_by_nurse(db)
    nurses: list[dict] = []
    async for conn in get_conn():
        rows = await get_approved_nurse_usernames(conn)
        break
    for r in rows:
        u = r["username"]
        nurses.append({
            "username": u,
            "full_name": r["full_name"],
            "online": nurse_is_online(u),
            "busy": nurse_is_busy(u, active_counts),
            "active_alerts": active_counts.get(u, 0),
        })
    return nurses


async def _gather_states(db, assigned: list[str]) -> dict[str, NurseState]:
    """Build the availability snapshot for a list of nurses from the live
    signaling registry, nurse WS manager, unacked-alert counts, and MANUAL_BUSY.
    """
    from database import get_active_alert_counts_by_nurse

    active_counts = await get_active_alert_counts_by_nurse(db)
    states: dict[str, NurseState] = {}
    for nurse in assigned:
        states[nurse] = NurseState(
            username=nurse,
            online=nurse_is_online(nurse),
            busy=nurse_is_busy(nurse, active_counts),
            active_alerts=active_counts.get(nurse, 0),
        )
    return states


async def _assigned_nurses(room_id: str) -> list[str]:
    from pg_database import get_conn, get_nurses_for_room
    async for conn in get_conn():
        return await get_nurses_for_room(conn, room_id)
    return []


async def route_alert(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    alert_id: int,
    payload_json: str,
    exclude: set[str] | None = None,
    is_reroute: bool = False,
) -> tuple[str | None, bool]:
    """Route an alert to the next available assigned nurse, or broadcast.

    `exclude` drops nurses from consideration (used by reroute to skip the nurse
    who didn't ack). `is_reroute` records it as a reroute (increments the count).

    Returns (routed_to_username_or_None, fell_back). Delivery side effects
    (sending over WebSocket, recording routed_to/at) happen here.
    """
    from routers import ws as ws_router
    from database import set_alert_routed_to, record_reroute

    exclude = exclude or set()

    # 1. Who is assigned to this room, in priority order (minus excluded)?
    assigned = [n for n in await _assigned_nurses(room_id) if n not in exclude]

    # 2. Snapshot availability.
    states = await _gather_states(db, assigned)

    # 3. Ask the pure scheduler.
    decision = choose_nurse(assigned, states)
    logger.info(
        "[ROUTE%s] alert=%d room=%s assigned=%s exclude=%s -> %s",
        "/reroute" if is_reroute else "", alert_id, room_id, assigned,
        sorted(exclude), decision.reason,
    )

    record = record_reroute if is_reroute else set_alert_routed_to
    manager = ws_router.manager

    # 4. Deliver.
    delivered = False
    if decision.chosen is not None:
        delivered = await manager.send_to_nurse(decision.chosen, payload_json)
        if not delivered:
            logger.warning(
                "[ROUTE] delivery to %s failed after selection — broadcasting",
                decision.chosen,
            )

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
    """Intermediary/dispatcher override: deliver an alert to a SPECIFIC nurse,
    bypassing the scheduler. Returns (delivered, message).

    If the target nurse has no live socket, we still record the redirect and
    broadcast as a fallback so the alert is not lost.
    """
    from routers import ws as ws_router
    from database import record_reroute

    manager = ws_router.manager
    delivered = await manager.send_to_nurse(target_nurse, payload_json)
    if delivered:
        await record_reroute(db, alert_id, target_nurse, False)
        logger.info("[REDIRECT] alert=%d manually sent to %s", alert_id, target_nurse)
        return True, f"Alert {alert_id} redirected to {target_nurse}"

    # Target offline — broadcast so it is not lost, but still record intent.
    await manager.broadcast(payload_json)
    await record_reroute(db, alert_id, None, True)
    logger.warning(
        "[REDIRECT] target %s offline for alert=%d — broadcast fallback",
        target_nurse, alert_id,
    )
    return False, f"{target_nurse} is offline; alert {alert_id} broadcast to all nurses instead"


async def reroute_stale(db: aiosqlite.Connection, *, older_than_seconds: int) -> int:
    """Find alerts routed to a specific nurse that remain unacked past the
    timeout and reroute each to the NEXT available nurse (excluding the one who
    didn't ack). Returns the number of alerts rerouted. Called by the background
    loop in main.py.
    """
    from database import get_stale_routed_unacked, get_alert_by_id
    from models import AlertResponse, WsAlertPayload

    stale = await get_stale_routed_unacked(db, older_than_seconds=older_than_seconds)
    rerouted = 0
    for row in stale:
        alert_id = row["id"]
        prev_nurse = row.get("routed_to")
        # Rebuild the push payload from the stored row.
        fresh = await get_alert_by_id(db, alert_id) or row
        payload_json = WsAlertPayload.from_alert_response(
            AlertResponse.from_db_row(fresh), event="alert_updated"
        ).model_dump_json()

        chosen, fell_back = await route_alert(
            db,
            room_id=row["room_id"],
            alert_id=alert_id,
            payload_json=payload_json,
            exclude={prev_nurse} if prev_nurse else set(),
            is_reroute=True,
        )
        logger.info(
            "[REROUTE] alert=%d was %s (no ack) -> %s fell_back=%s",
            alert_id, prev_nurse, chosen, fell_back,
        )
        rerouted += 1
    return rerouted
