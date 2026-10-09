"""
routing.py — connect alert ingestion to nurse delivery.

Simplified routing (no scheduler / no escalation):
Both alert entry points (HTTP POST /audio/ingest and the patient WebSocket)
call `route_alert`. It delivers the alert to the nurses ASSIGNED to the
patient's room. If none of the assigned nurses are reachable (or the room has
no assignment), it broadcasts to all nurses so the request is never dropped.

The admin dispatcher can afterwards reassign a specific alert to a chosen nurse
via `redirect_alert` (POST /alerts/{id}/redirect).

There is no acuity (ESI), competency matching, live-status gating, or
primary→secondary→supervisor escalation — those were removed. "Busy" is tracked
only as a manual admin toggle, surfaced on the dispatch board for visibility.
"""

import logging

import aiosqlite

logger = logging.getLogger(__name__)

# Manual busy toggle: usernames a nurse (or admin) marked unavailable.
# Advisory only — surfaced on the dispatch board; does not block delivery.
MANUAL_BUSY: set[str] = set()


def set_manual_busy(nurse: str, busy: bool) -> None:
    if busy:
        MANUAL_BUSY.add(nurse)
    else:
        MANUAL_BUSY.discard(nurse)


def nurse_is_busy(nurse: str, active_counts: dict[str, int]) -> bool:
    """Advisory 'busy' for the admin board: on a call, holding an unacked
    alert, or manually marked busy."""
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
    """Per-nurse availability for the admin dispatch board (free/busy + online)."""
    from pg_database import get_conn, get_nurse_routing_map
    from database import get_active_alert_counts_by_nurse

    active_counts = await get_active_alert_counts_by_nurse(db)
    async for conn in get_conn():
        routing_map = await get_nurse_routing_map(conn)
        break

    from routers.signal import registry as signal_registry

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
        })
    return nurses


async def route_alert(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    alert_id: int,
    payload_json: str,
    exclude: set[str] | None = None,
    is_reroute: bool = False,
    allow_supervisor: bool = True,  # kept for call-site compatibility; unused
) -> tuple[str | None, bool]:
    """Deliver an alert to the nurses assigned to the patient's room.

    Behaviour:
      • Send to every assigned nurse that is online.
      • If no assigned nurse is online (or the room has no assignment), broadcast
        to all nurses as a fallback.
    Returns (routed_to_or_None, fell_back). `routed_to` is set to the first
    assigned nurse delivered to (for display/audit); None when broadcast.
    """
    from routers import ws as ws_router
    from database import set_alert_routed_to, record_reroute
    from pg_database import get_conn, get_nurses_for_room

    exclude = exclude or set()

    async for conn in get_conn():
        assigned_all = await get_nurses_for_room(conn, room_id)
        break

    assigned = [n for n in assigned_all if n not in exclude]
    manager = ws_router.manager
    record = record_reroute if is_reroute else set_alert_routed_to

    # Deliver to every online assigned nurse; remember the first success.
    first_delivered: str | None = None
    for nurse in assigned:
        if await manager.send_to_nurse(nurse, payload_json):
            if first_delivered is None:
                first_delivered = nurse

    logger.info(
        "[ROUTE%s] alert=%d room=%s assigned=%s delivered_to=%s",
        "/reroute" if is_reroute else "", alert_id, room_id, assigned, first_delivered,
    )

    if first_delivered is None:
        # Nobody assigned reachable → broadcast so it's never dropped.
        await manager.broadcast(payload_json)
        await record(db, alert_id, None, True)
        return None, True

    await record(db, alert_id, first_delivered, False)
    return first_delivered, False


async def redirect_alert(
    db: aiosqlite.Connection,
    *,
    alert_id: int,
    target_nurse: str,
    payload_json: str,
) -> tuple[bool, str]:
    """Dispatcher override: deliver an alert to a SPECIFIC nurse.
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
