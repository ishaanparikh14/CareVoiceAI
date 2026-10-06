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
from fastapi import APIRouter, Depends, HTTPException, Query, status
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
):
    from database import insert_alert
    from models import Priority, Intent, AlertResponse, WsAlertPayload
    from routers.audio import _ws_broadcast

    room_id = room_id.strip() or "Unknown"

    transcript = f"[MANUAL CALL] {patient_name} in Room {room_id} pressed the Call Nurse button"

    alert_id = await insert_alert(
        db,
        room_id        = room_id,
        priority       = Priority.CRITICAL.value,
        intent         = Intent.EMERGENCY.value,
        distress_score = 0.0,   # distress_score is deprecated; not used in display or logic
        transcript     = transcript,
        wav_path       = None,
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
    )
    if _ws_broadcast is not None:
        payload = WsAlertPayload.from_alert_response(alert_resp)
        await _ws_broadcast(payload.model_dump_json())

    logger.info("Manual alert created: id=%d room=%s patient=%s", alert_id, room_id, patient_name)

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
    db: aiosqlite.Connection = Depends(get_db),
):
    rows = await get_latest_alerts(db, limit=limit, unacked_only=unacked_only)
    alerts = [AlertResponse.from_db_row(row) for row in rows]

    logger.debug(
        "GET /alerts/latest — returned %d alerts (unacked_only=%s)",
        len(alerts), unacked_only,
    )

    return AlertListResponse(
        alerts       = alerts,
        total        = len(alerts),
        unacked_only = unacked_only,
    )


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


# ── POST /alerts/status ───────────────────────────────────────────────────────
# A nurse sets their live location/status. The scheduler checks this before
# routing time-critical requests (on-break / off-duty are skipped).

class StatusRequest(BaseModel):
    status: str = Field(..., description="available | in_patient_room | on_break | off_duty")


@router.post("/status", summary="Nurse: set live location/status")
async def set_status(
    body:         StatusRequest,
    current_user: CurrentUser,
):
    if current_user["role"] != "nurse":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Only a nurse can set status")
    from pg_database import get_conn, set_nurse_status
    ok = False
    async for conn in get_conn():
        ok = await set_nurse_status(conn, current_user["username"], body.status)
        break
    if not ok:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Invalid status value")
    logger.info("Nurse %s status=%s", current_user["username"], body.status)
    return {"username": current_user["username"], "status": body.status}
