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


async def route_alert(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    alert_id: int,
    payload_json: str,
) -> tuple[str | None, bool]:
    """Route an alert to the next available assigned nurse, or broadcast.

    Returns (routed_to_username_or_None, fell_back). Delivery side effects
    (sending over WebSocket, recording routed_to) happen here.
    """
    # Imported here to avoid import cycles at module load.
    from routers import ws as ws_router
    from routers.signal import registry as signal_registry
    from pg_database import get_conn, get_nurses_for_room
    from database import get_active_alert_counts_by_nurse, set_alert_routed_to

    # 1. Who is assigned to this room, in priority order?
    assigned: list[str] = []
    async for conn in get_conn():
        assigned = await get_nurses_for_room(conn, room_id)
        break

    # 2. How many unacked alerts does each nurse already hold? (busy + tiebreak)
    active_counts = await get_active_alert_counts_by_nurse(db)

    # 3. Build the availability snapshot for each assigned nurse.
    manager = ws_router.manager
    states: dict[str, NurseState] = {}
    for nurse in assigned:
        on_call = signal_registry.is_busy(nurse)
        holding = active_counts.get(nurse, 0) > 0
        manual  = nurse in MANUAL_BUSY
        states[nurse] = NurseState(
            username=nurse,
            online=manager.is_online(nurse),
            busy=on_call or holding or manual,
            active_alerts=active_counts.get(nurse, 0),
        )

    # 4. Ask the pure scheduler.
    decision = choose_nurse(assigned, states)
    logger.info(
        "[ROUTE] alert=%d room=%s assigned=%s -> %s",
        alert_id, room_id, assigned, decision.reason,
    )

    # 5. Deliver.
    delivered = False
    if decision.chosen is not None:
        delivered = await manager.send_to_nurse(decision.chosen, payload_json)
        if not delivered:
            # The nurse looked online but the socket died between snapshot and
            # send — fall back to broadcast rather than lose the alert.
            logger.warning(
                "[ROUTE] delivery to %s failed after selection — broadcasting",
                decision.chosen,
            )

    if decision.chosen is None or not delivered:
        await manager.broadcast(payload_json)
        await set_alert_routed_to(db, alert_id, None, True)
        return None, True

    await set_alert_routed_to(db, alert_id, decision.chosen, False)
    return decision.chosen, False
