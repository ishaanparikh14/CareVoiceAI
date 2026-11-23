"""
routers/alerts.py — alert retrieval and acknowledgement endpoints.

Endpoints
---------
GET  /alerts/latest           — list recent alerts (nurse dashboard poll)
GET  /alerts/{alert_id}       — single alert detail
POST /alerts/{alert_id}/ack   — mark an alert as acknowledged

The nurse phone app can either hold a WebSocket open (preferred for real-time)
or poll GET /alerts/latest on a timer.  Both paths are supported so the system
degrades gracefully if WebSocket connectivity is unavailable on the ward network.
"""

import logging

import aiosqlite
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from auth import CurrentUser
from database import (
    acknowledge_alert,
    get_alert_by_id,
    get_latest_alerts,
    get_db,
    utcnow,
)
from models import AckRequest, AckResponse, AlertListResponse, AlertResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/alerts", tags=["Alerts"])

# request_key -> alert_id for recent manual calls (bounded; single process).
_MANUAL_KEYS: dict[str, dict[str, int]] = {}


def _remember_key(key: str, alert_id: int) -> None:
    store = _MANUAL_KEYS.setdefault("_", {})
    store[key] = alert_id
    if len(store) > 500:
        for k in list(store)[:250]:
            store.pop(k, None)



# ── POST /alerts/manual ───────────────────────────────────────────────────────

@router.post(
    "/manual",
    response_model=AckResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Patient manually calls the nurse (no audio)",
    description=(
        "Called when the patient taps the 'Call Nurse' button without speaking. "
        "Creates a Critical/Emergency alert immediately and broadcasts it to all "
        "connected nurse WebSocket clients."
    ),
)
async def manual_alert(
    db: aiosqlite.Connection = Depends(get_db),
    room_id: str = Query(..., description="Patient room identifier, e.g. 4B"),
    patient_name: str = Query(default="Patient", description="Patient's full name"),
    request_key: str = Query(default="", description="Idempotency key from the app's retry queue"),
):
    from database import insert_alert
    from models import Priority, Intent, AlertResponse, WsAlertPayload
    from routers.audio import _ws_broadcast

    room_id = room_id.strip() or "Unknown"

    # Idempotency: the app retries queued Call Nurse taps after a network drop.
    # A repeat with the same key returns the original alert instead of paging
    # the nurse twice.
    if request_key:
        _MANUAL_KEYS.setdefault("_", {})
        prev = _MANUAL_KEYS["_"].get(request_key)
        if prev is not None:
            return AckResponse(alert_id=prev, acknowledged=False, ack_by="", ack_at=utcnow(),
                               message="Already received")

    # Resolve the real patient name for the room when the caller didn't pass one.
    resolved_name = patient_name
    if not resolved_name or resolved_name == "Patient":
        from pg_database import get_patient_name_by_room
        resolved_name = (await get_patient_name_by_room(room_id)) or "Patient"

    transcript = f"[MANUAL CALL] {resolved_name} in Room {room_id} pressed the Call Nurse button"
    summary    = "pressed the Call Nurse button — needs immediate help"
    emotion    = "distressed"
    alert_message = f"Patient {resolved_name} in Room {room_id} is calling — manual emergency call — {summary}."

    alert_id = await insert_alert(
        db,
        room_id        = room_id,
        priority       = Priority.CRITICAL.value,
        intent         = Intent.EMERGENCY.value,
        distress_score = 0.0,   # distress_score is deprecated; not used in display or logic
        transcript     = transcript,
        wav_path       = None,
        patient_name   = resolved_name,
        summary        = summary,
        emotion        = emotion,
        alert_message  = alert_message,
    )

    # Broadcast to all connected nurses via WebSocket
    now = utcnow()
    alert_resp = AlertResponse(
        id             = alert_id,
        room_id        = room_id,
        priority       = Priority.CRITICAL,
        intent         = Intent.EMERGENCY,
        distress_score = 0.0,
        transcript     = transcript,
        acknowledged   = False,
        created_at     = now,
        patient_name   = resolved_name,
        summary        = summary,
        emotion        = emotion,
        alert_message  = alert_message,
    )
    # Route through the dispatch engine (same path as audio alerts): offer to
    # the primary if available, otherwise broadcast to all FREE nurses, and
    # escalate to the admin if nobody is free. A raw broadcast here would bypass
    # dispatch entirely (showing "Not routed" and never reaching free nurses
    # when the primary is offline) — the bug this fixes.
    payload = WsAlertPayload.from_alert_response(alert_resp).model_dump_json()
    try:
        from routing import route_alert
        routed_to, fell_back = await route_alert(
            db, room_id=room_id, alert_id=alert_id, payload_json=payload,
        )
        logger.info("Manual alert %d routed_to=%s fell_back=%s", alert_id, routed_to, fell_back)
    except Exception as exc:
        logger.warning("Manual alert dispatch failed (%s) — raw broadcast fallback", exc)
        if _ws_broadcast is not None:
            await _ws_broadcast(payload)

    logger.info("Manual alert created: id=%d room=%s patient=%s", alert_id, room_id, resolved_name)
    if request_key:
        _remember_key(request_key, alert_id)

    return AckResponse(
        alert_id     = alert_id,
        acknowledged = False,
        ack_by       = "",
        ack_at       = now,
        message      = "Manual alert sent to nurses",
    )


# ── GET /alerts/latest ────────────────────────────────────────────────────────

@router.get(
    "/latest",
    response_model=AlertListResponse,
    summary="List recent alerts for the nurse dashboard",
    description=(
        "Returns alerts ordered by creation time descending.  "
        "Use `unacked_only=true` to fetch only pending (unacknowledged) alerts.  "
        "The nurse app polls this endpoint as a fallback when WebSocket is unavailable."
    ),
)
async def list_latest_alerts(
    limit:        int  = Query(default=20, ge=1, le=100,
                               description="Maximum number of alerts to return"),
    unacked_only: bool = Query(default=False,
                               description="If true, return only unacknowledged alerts"),
    authorization: str | None = Header(default=None),
    db: aiosqlite.Connection = Depends(get_db),
):
    # A signed-in NURSE gets only the alerts the dispatch engine gave her
    # (offered/assigned to her, or broadcast while she is free, or her own
    # patients' finished/unpaged alerts). Admins and unauthenticated callers
    # keep the full list.
    nurse = None
    if authorization and authorization.lower().startswith("bearer "):
        try:
            from auth import decode_token
            claims = decode_token(authorization.split(" ", 1)[1].strip())
            if claims.get("role") == "nurse":
                nurse = claims.get("sub")
        except Exception:
            nurse = None

    if nurse is None:
        rows = await get_latest_alerts(db, limit=limit, unacked_only=unacked_only)
    else:
        rows = await _scoped_alerts_for_nurse(db, nurse, limit=limit, unacked_only=unacked_only)
    alerts = [AlertResponse.from_db_row(row) for row in rows]

    logger.debug(
        "GET /alerts/latest — returned %d alerts (unacked_only=%s)",
        len(alerts), unacked_only,
    )

    return AlertListResponse(
        alerts       = alerts,
        total        = len(alerts),
        unacked_only = unacked_only,
        scoped       = nurse is not None,
    )


async def _scoped_alerts_for_nurse(db: aiosqlite.Connection, nurse: str, *, limit: int,
                                   unacked_only: bool) -> list:
    import dispatch
    from pg_database import get_conn, get_patients_for_nurse
    my_rooms: set[str] = set()
    try:
        async for conn in get_conn():
            my_rooms = {(p["room_number"] or "").strip() for p in await get_patients_for_nurse(conn, nurse)}
            break
    except Exception as exc:
        logger.warning("Room lookup failed for %s (%s) — showing only dispatched alerts", nurse, exc)
    candidates = await get_latest_alerts(db, limit=min(limit * 4, 300), unacked_only=unacked_only)
    ids = [r["id"] for r in candidates]
    rows_by_alert: dict[int, dict] = {}
    if ids:
        marks = ",".join("?" * len(ids))
        async with db.execute(f"SELECT * FROM dispatch_requests WHERE alert_id IN ({marks})", ids) as cur:
            rows_by_alert = {r["alert_id"]: dict(r) for r in await cur.fetchall()}
    i_am_free = await dispatch.is_available(db, nurse)
    out = []
    for r in candidates:
        if await dispatch.nurse_can_see(db, nurse, my_rooms, dict(r), rows_by_alert.get(r["id"]),
                                        i_am_free=i_am_free):
            out.append(r)
            if len(out) >= limit:
                break
    return out


# ── GET /alerts/{alert_id} ────────────────────────────────────────────────────

@router.get(
    "/{alert_id}",
    response_model=AlertResponse,
    summary="Get a single alert by ID",
)
async def get_alert(
    alert_id: int,
    db: aiosqlite.Connection = Depends(get_db),
):
    row = await get_alert_by_id(db, alert_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Alert {alert_id} not found",
        )
    return AlertResponse.from_db_row(row)


# ── POST /alerts/{alert_id}/ack ───────────────────────────────────────────────

@router.post(
    "/{alert_id}/ack",
    response_model=AckResponse,
    summary="Acknowledge an alert",
    description=(
        "Called by the nurse app when the nurse taps the ACK button.  "
        "Idempotent in the sense that a 404 is returned for already-acknowledged "
        "alerts so the nurse app can detect duplicate taps."
    ),
)
async def ack_alert(
    alert_id: int,
    body:     AckRequest,
    db:       aiosqlite.Connection = Depends(get_db),
):
    # Verify alert exists first so we can return 404 vs 409 correctly.
    row = await get_alert_by_id(db, alert_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Alert {alert_id} not found",
        )

    if row["acknowledged"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Alert {alert_id} is already acknowledged by '{row['ack_by']}'",
        )

    updated = await acknowledge_alert(db, alert_id, ack_by=body.ack_by)

    if not updated:
        # Race condition: another nurse ACK'd between our read and write.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Alert {alert_id} was acknowledged by another nurse simultaneously",
        )

    now = utcnow()
    logger.info("Alert %d acknowledged by %s", alert_id, body.ack_by)

    # Resolve the dispatch request (cancels timers, clears the card from other
    # nurses) in the BACKGROUND with its own DB connection, so the ACK answers
    # immediately instead of waiting on WebSocket fan-out — a slow socket was
    # one cause of the "ACK button freezes" report.
    await db.commit()
    import asyncio

    async def _resolve_bg(aid: int, who: str):
        try:
            from config import settings as _s
            from dispatch import on_alert_acknowledged
            async with aiosqlite.connect(_s.DB_PATH) as bg:
                bg.row_factory = aiosqlite.Row
                await bg.execute("PRAGMA busy_timeout=5000")
                await on_alert_acknowledged(bg, aid, who)
        except Exception as exc:
            logger.warning("Dispatch resolve failed for alert %d: %s", aid, exc)

    asyncio.create_task(_resolve_bg(alert_id, body.ack_by))

    return AckResponse(
        alert_id     = alert_id,
        acknowledged = True,
        ack_by       = body.ack_by,
        ack_at       = now,
    )


# ── POST /alerts/{alert_id}/redirect ──────────────────────────────────────────
# Intermediary / dispatcher override: a charge nurse or admin manually sends a
# specific alert to a specific nurse, bypassing the automatic scheduler.

class RedirectRequest(BaseModel):
    target_nurse: str = Field(..., description="Username of the nurse to receive this alert")


class RedirectResponse(BaseModel):
    alert_id:     int
    delivered:    bool
    target_nurse: str
    message:      str


@router.post(
    "/{alert_id}/redirect",
    response_model=RedirectResponse,
    summary="Dispatcher: manually redirect an alert to a specific nurse",
    description=(
        "Lets an intermediary (admin or any nurse acting as dispatcher) override "
        "the automatic scheduler and send this alert to a chosen nurse. If that "
        "nurse is offline, the alert is broadcast to all nurses as a fallback."
    ),
)
async def redirect_alert_endpoint(
    alert_id:     int,
    body:         RedirectRequest,
    current_user: CurrentUser,
    db:           aiosqlite.Connection = Depends(get_db),
):
    # Only an admin or a nurse may act as the intermediary/dispatcher.
    if current_user["role"] not in ("admin", "nurse"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Only a nurse or admin can redirect alerts")

    row = await get_alert_by_id(db, alert_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Alert {alert_id} not found")
    if row["acknowledged"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"Alert {alert_id} is already acknowledged — nothing to redirect")

    target = body.target_nurse.strip()
    if not target:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="target_nurse must not be empty")

    # Validate the target is a real, approved nurse.
    from pg_database import get_conn, get_user_by_username
    async for conn in get_conn():
        target_user = await get_user_by_username(conn, target)
        break
    if target_user is None or target_user["role"] != "nurse":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"'{target}' is not a nurse")

    from models import WsAlertPayload
    payload_json = WsAlertPayload.from_alert_response(
        AlertResponse.from_db_row(row), event="alert_updated"
    ).model_dump_json()

    from routing import redirect_alert
    delivered, message = await redirect_alert(
        db, alert_id=alert_id, target_nurse=target, payload_json=payload_json,
    )
    logger.info("Alert %d redirected by %s (%s) -> %s delivered=%s",
                alert_id, current_user["username"], current_user["role"], target, delivered)

    return RedirectResponse(
        alert_id=alert_id, delivered=delivered, target_nurse=target, message=message,
    )


# ── POST /alerts/busy ─────────────────────────────────────────────────────────
# A nurse marks themselves available/unavailable; the scheduler skips busy nurses.

class BusyRequest(BaseModel):
    busy: bool = Field(..., description="True = mark me unavailable, False = available")


@router.post(
    "/busy",
    summary="Nurse: toggle manual busy/available state",
    description="A nurse marks themselves unavailable so the scheduler routes to someone else.",
)
async def set_busy(
    body:         BusyRequest,
    current_user: CurrentUser,
):
    if current_user["role"] != "nurse":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Only a nurse can set busy state")
    from routing import set_manual_busy
    set_manual_busy(current_user["username"], body.busy)
    logger.info("Nurse %s manual busy=%s", current_user["username"], body.busy)
    return {"username": current_user["username"], "busy": body.busy}
