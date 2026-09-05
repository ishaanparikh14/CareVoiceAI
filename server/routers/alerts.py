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
        distress_score = 1.0,
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
        distress_score = 1.0,
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
