"""
routing.py — connect alert ingestion to nurse delivery (Uber-style dispatch).

Alerts enter here from both the HTTP POST /audio/ingest path and the manual
call endpoint. `route_alert` now hands the alert to the dispatch engine
(dispatch.py), which runs the state machine:

    primary nurse → (decline / offline) → broadcast to FREE nurses
                  → first FREE accept wins
                  → no taker in 10s → notify admin → admin reassigns (FREE only)

This module keeps:
  • presence/status helpers (presence = nurse-alert WebSocket ONLY, so logout is
    authoritative and a logged-out nurse shows OFFLINE, never FREE),
  • the manual "busy" toggle set,
  • `nurse_availability` for the admin dispatch board (now reporting the explicit
    available/busy/offline status),
  • `redirect_alert`, which is the admin override and routes through the dispatch
    engine so it can only land on an AVAILABLE nurse.
"""

import logging

import aiosqlite

logger = logging.getLogger(__name__)

# Manual busy toggle: usernames a nurse (or admin) marked unavailable.
# A manually-busy nurse is treated as BUSY and is skipped by the broadcast.
MANUAL_BUSY: set[str] = set()


def set_manual_busy(nurse: str, busy: bool) -> None:
    if busy:
        MANUAL_BUSY.add(nurse)
    else:
        MANUAL_BUSY.discard(nurse)


def nurse_is_online(nurse: str) -> bool:
    """Online == has a live nurse-alert WebSocket.

    Presence is deliberately NOT OR'd with the signaling registry anymore: a
    logged-out nurse cancels her alert socket, so she must read as OFFLINE even
    if a signaling socket lingers briefly. This fixes the "logged out but shown
    FREE" bug on the dispatch board.
    """
    from routers import ws as ws_router
    return ws_router.manager.is_online(nurse)


async def nurse_availability(db) -> list[dict]:
    """Per-nurse availability for the admin dispatch board.

    Reports the explicit three-state status (available/busy/offline) plus the
    booleans the existing admin.html expects (online/busy) so the board keeps
    working while it is upgraded.
    """
    from pg_database import get_conn, get_nurse_routing_map
    from database import get_active_alert_counts_by_nurse, count_active_dispatch_by_nurse
    from dispatch import STATUS_OFFLINE, STATUS_AVAILABLE, STATUS_BUSY

    active_counts = await get_active_alert_counts_by_nurse(db)
    disp_counts = await count_active_dispatch_by_nurse(db)
    async for conn in get_conn():
        routing_map = await get_nurse_routing_map(conn)
        break

    from routers.signal import registry as signal_registry

    nurses: list[dict] = []
    for u, info in sorted(routing_map.items(), key=lambda kv: kv[1]["full_name"]):
        online = nurse_is_online(u)
        held_alerts = active_counts.get(u, 0)
        held_disp = disp_counts.get(u, 0)
        on_call = signal_registry.is_busy(u)
        manual = u in MANUAL_BUSY
        busy = online and (on_call or held_alerts > 0 or held_disp > 0 or manual)

        if not online:
            status = STATUS_OFFLINE
        elif busy:
            status = STATUS_BUSY
        else:
            status = STATUS_AVAILABLE

        reasons = []
        if on_call:
            reasons.append("On a call")
        total_held = held_alerts + held_disp
        if total_held > 0:
            reasons.append(f"Handling {total_held} request{'s' if total_held != 1 else ''}")
        if manual:
            reasons.append("Marked busy")

        nurses.append({
            "username": u,
            "full_name": info["full_name"],
            "ward": info["ward"],
            "online": online,
            "status": status,                 # "available" | "busy" | "offline"
            "available": status == STATUS_AVAILABLE,
            "busy": busy,
            "manual_busy": manual,
            "busy_reason": " · ".join(reasons),
            "active_alerts": total_held,
        })
    return nurses


async def route_alert(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    alert_id: int,
    payload_json: str,          # kept for call-site compatibility (unused now)
    exclude: set[str] | None = None,
    is_reroute: bool = False,
    allow_supervisor: bool = True,  # kept for call-site compatibility; unused
) -> tuple[str | None, bool]:
    """Hand a freshly created alert to the dispatch engine.

    Returns (routed_to_or_None, fell_back) for backwards-compatible logging:
      • routed_to = the primary nurse when the alert was offered to her,
      • None + fell_back=True when it broadcast to free nurses immediately.
    """
    from database import get_alert_by_id
    from dispatch import start_dispatch

    alert = await get_alert_by_id(db, alert_id)
    if alert is None:
        logger.warning("[ROUTE] alert %d missing — cannot dispatch", alert_id)
        return None, True

    result = await start_dispatch(db, alert)
    state = result.get("state")
    if state == "pending_primary":
        return result.get("primary"), False
    return None, True


async def redirect_alert(
    db: aiosqlite.Connection,
    *,
    alert_id: int,
    target_nurse: str,
    payload_json: str,          # kept for call-site compatibility (unused now)
) -> tuple[bool, str]:
    """Admin/dispatcher override: assign an alert to a SPECIFIC nurse.

    Routes through the dispatch engine so the target must be AVAILABLE; an
    offline or busy nurse is rejected (the admin board only offers available
    nurses, and this re-validates at click time)."""
    from database import get_dispatch_by_alert, get_alert_by_id
    from dispatch import admin_reassign, start_dispatch

    alert = await get_alert_by_id(db, alert_id)
    if alert is None:
        return False, f"Alert {alert_id} not found"

    row = await get_dispatch_by_alert(db, alert_id)
    if row is None:
        # No dispatch request yet (legacy alert) — start one, then reassign.
        await start_dispatch(db, alert)
        row = await get_dispatch_by_alert(db, alert_id)
    if row is None:
        return False, "Could not create a dispatch request for this alert"

    res = await admin_reassign(db, row["id"], target_nurse)
    if res.get("ok"):
        logger.info("[REDIRECT] alert=%d assigned to %s", alert_id, target_nurse)
        return True, f"Alert {alert_id} assigned to {target_nurse}"

    err = res.get("error", "failed")
    if err == "nurse_unavailable":
        return False, f"{target_nurse} is offline — they must be logged in to receive the request"
    if err in ("already_resolved", "already_handled", "alert_gone"):
        return False, f"Alert {alert_id} was already handled"
    return False, f"Could not reassign alert {alert_id}: {err}"
