"""PostgreSQL-backed alert retrieval and state transitions."""
import logging
import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from auth import CurrentUser
from pg_database import get_conn

from database import (
    acknowledge_alert, attend_alert, get_alert_by_id, get_latest_alerts,
    get_db, insert_alert, utcnow,
)
from models import AckRequest, AckResponse, AlertListResponse, AlertResponse, AttendRequest, AttendResponse, Priority, Intent, WsAlertPayload, WsAlertUpdate

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/alerts", tags=["Alerts"])


def _alert(row):
    return AlertResponse.from_db_row(row)


@router.post("/manual", response_model=AckResponse, status_code=status.HTTP_201_CREATED)
async def manual_alert(
    db: asyncpg.Connection = Depends(get_db),
    room_id: str = Query(...),
    patient_name: str = Query(default="Patient"),
):
    from routers.audio import _ws_broadcast
    room_id = room_id.strip()
    if not room_id:
        raise HTTPException(422, "room_id is required")
    transcript = f"[MANUAL CALL] {patient_name} in Room {room_id} is calling the nurse"
    alert_id = await insert_alert(
        db, room_id=room_id, patient_name=patient_name,
        priority=Priority.CRITICAL.value, intent=Intent.EMERGENCY.value,
        distress_score=0.0, transcript=transcript, wav_path=None,
    )
    row = await get_alert_by_id(db, alert_id)
    alert = _alert(row)
    if _ws_broadcast:
        await _ws_broadcast(WsAlertPayload.from_alert_response(alert).model_dump_json())
    return AckResponse(alert_id=alert_id, acknowledged=False, ack_by="", ack_at=alert.created_at, message="Manual alert sent to nurses")


@router.get("/latest", response_model=AlertListResponse)
async def list_latest_alerts(
    limit: int = Query(default=20, ge=1, le=100),
    unacked_only: bool = Query(default=False),
    db: asyncpg.Connection = Depends(get_db),
):
    rows = await get_latest_alerts(db, limit, unacked_only)
    alerts = [_alert(r) for r in rows]
    return AlertListResponse(alerts=alerts, total=len(alerts), unacked_only=unacked_only)

@router.get(
    "/patient/latest",
    response_model=AlertResponse | None,
    summary="Get the latest alert status for a patient",
)
async def get_patient_latest_alert(
    current_user: CurrentUser,
    room_id: str,
    db: asyncpg.Connection = Depends(get_conn),
):
    if current_user["role"] != "patient":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Patient role required",
        )

    room_id = room_id.strip()

    if not room_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="room_id is required",
        )

    row = await db.fetchrow(
        """
        SELECT *
        FROM alerts
        WHERE room_id = $1
        ORDER BY created_at DESC
        LIMIT 1
        """,
        room_id,
    )

    if row is None:
        return None

    return AlertResponse.from_db_row(row)

@router.get("/{alert_id}", response_model=AlertResponse)
async def get_alert(alert_id: int, db: asyncpg.Connection = Depends(get_db)):
    row = await get_alert_by_id(db, alert_id)
    if row is None:
        raise HTTPException(404, f"Alert {alert_id} not found")
    return _alert(row)


@router.post("/{alert_id}/ack", response_model=AckResponse)
async def ack_alert(alert_id: int, body: AckRequest, db: asyncpg.Connection = Depends(get_db)):
    row = await get_alert_by_id(db, alert_id)
    if row is None:
        raise HTTPException(404, f"Alert {alert_id} not found")
    if row["acknowledged"]:
        raise HTTPException(409, f"Alert {alert_id} is already acknowledged by '{row['ack_by']}'")
    if not await acknowledge_alert(db, alert_id, body.ack_by):
        raise HTTPException(409, f"Alert {alert_id} was acknowledged simultaneously by another nurse")
    updated = await get_alert_by_id(db, alert_id)
    alert = _alert(updated)
    from routers.audio import _ws_broadcast
    if _ws_broadcast:
        await _ws_broadcast(WsAlertUpdate.from_alert_response(alert).model_dump_json())
    return AckResponse(alert_id=alert_id, acknowledged=True, ack_by=body.ack_by, ack_at=alert.ack_at or utcnow())


@router.post("/{alert_id}/attend", response_model=AttendResponse)
async def attend_alert_endpoint(alert_id: int, body: AttendRequest, db: asyncpg.Connection = Depends(get_db)):
    row = await get_alert_by_id(db, alert_id)
    if row is None:
        raise HTTPException(404, f"Alert {alert_id} not found")
    if row["attended"]:
        raise HTTPException(409, f"Alert {alert_id} is already attended by '{row['attended_by']}'")
    if not await attend_alert(db, alert_id, body.attended_by):
        raise HTTPException(409, f"Alert {alert_id} was attended simultaneously by another nurse")
    updated = await get_alert_by_id(db, alert_id)
    alert = _alert(updated)
    from routers.audio import _ws_broadcast
    if _ws_broadcast:
        await _ws_broadcast(WsAlertUpdate.from_alert_response(alert).model_dump_json())
    return AttendResponse(alert_id=alert_id, attended=True, attended_by=body.attended_by, attended_at=alert.attended_at or utcnow())
