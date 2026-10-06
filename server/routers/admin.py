"""
routers/admin.py — Admin-only monitoring endpoints.

All routes require the 'admin' role (AdminUser dependency).

Endpoints
---------
GET  /admin/stats              — Overview counts (nurses, patients, rooms, alerts)
GET  /admin/nurses             — Full list of all nurses
GET  /admin/patients           — Full list of all patients (including discharged)
GET  /admin/rooms              — Room-level view: patient + attending nurse per room
GET  /admin/alerts             — All alerts with filters (limit, priority, room, unacked)
GET  /admin/alerts/stats       — Alert aggregates: counts by priority, by room, by intent
GET  /admin/logs               — Last N lines of the server log file
"""

import logging
from typing import Any

import aiosqlite
import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from pydantic import BaseModel, Field

from auth import AdminUser
from config import settings
from database import get_db
from pg_database import (
    get_all_nurses,
    get_all_patients_admin,
    get_admin_stats,
    get_room_overview,
    get_conn,
    get_nurses_by_status,
    set_nurse_approval,
    get_all_rooms,
    create_room,
    update_room,
    assign_patient_nurse,
    set_patient_discharge,
    get_approved_nurse_usernames,
    get_room_status_summary,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["Admin"])

# Path to the server log file (written by uvicorn / logging basicConfig)
_LOG_FILE = settings.BASE_DIR / "server.log"


# ── GET /admin/stats ──────────────────────────────────────────────────────────

@router.get("/stats", summary="Hospital overview counts")
async def admin_stats(
    _admin: AdminUser,
    conn: asyncpg.Connection = Depends(get_conn),
    db:   aiosqlite.Connection = Depends(get_db),
):
    """Overview cards: nurses, patients, rooms, total alerts, unacked alerts."""
    pg_stats = await get_admin_stats(conn)

    # SQLite alert counts
    async with db.execute("SELECT COUNT(*) FROM alerts") as cur:
        total_alerts = (await cur.fetchone())[0]
    async with db.execute("SELECT COUNT(*) FROM alerts WHERE acknowledged=0") as cur:
        unacked_alerts = (await cur.fetchone())[0]
    async with db.execute("SELECT COUNT(*) FROM alerts WHERE priority='Critical'") as cur:
        critical_alerts = (await cur.fetchone())[0]

    return {
        **pg_stats,
        "total_alerts":    total_alerts,
        "unacked_alerts":  unacked_alerts,
        "critical_alerts": critical_alerts,
    }


# ── GET /admin/dispatch-board ─────────────────────────────────────────────────
# Live view for the admin dispatcher: which nurses are free/occupied + the
# current unacknowledged alerts and where each was routed. The Android admin
# app polls this and uses POST /alerts/{id}/redirect to reassign.

@router.get("/dispatch-board", summary="Dispatcher board: nurse availability + pending alerts")
async def dispatch_board(
    _admin: AdminUser,
    db: aiosqlite.Connection = Depends(get_db),
    conn: asyncpg.Connection = Depends(get_conn),
):
    from routing import nurse_availability, required_competency_for, is_time_critical

    nurses = await nurse_availability(db)

    # Room → ESI acuity map (from Postgres) to annotate each request.
    acuity_rows = await conn.fetch(
        "SELECT room_number, acuity FROM patients WHERE is_discharged = FALSE"
    )
    acuity_by_room = {r["room_number"]: r["acuity"] for r in acuity_rows}

    # Pending (unacknowledged) alerts, newest first.
    query = """
        SELECT id, room_id, patient_name, priority, intent, summary,
               created_at, routed_to, fell_back, reroute_count
        FROM alerts
        WHERE acknowledged = 0
        ORDER BY created_at DESC
        LIMIT 100
    """
    async with db.execute(query) as cur:
        rows = await cur.fetchall()
    alerts = []
    for r in rows:
        esi = acuity_by_room.get(r["room_id"])
        alerts.append({
            "id":            r["id"],
            "room_id":       r["room_id"],
            "patient_name":  r["patient_name"],
            "priority":      r["priority"],
            "intent":        r["intent"],
            "summary":       r["summary"],
            "created_at":    r["created_at"],
            "routed_to":     r["routed_to"],
            "fell_back":     bool(r["fell_back"]),
            "reroute_count": r["reroute_count"],
            "acuity":        esi,
            "required_competency": required_competency_for(r["intent"], r["priority"], esi),
            "time_critical": is_time_critical(r["priority"], esi),
        })

    return {"nurses": nurses, "pending_alerts": alerts}


# ── POST /admin/nurses/{username}/busy ────────────────────────────────────────
# Dispatcher marks a nurse occupied/free; the scheduler skips occupied nurses.

class AdminBusyRequest(BaseModel):
    busy: bool = Field(..., description="True = occupied, False = free")


@router.post("/nurses/{username}/busy", summary="Dispatcher: mark a nurse occupied or free")
async def admin_set_nurse_busy(
    username: str,
    body: AdminBusyRequest,
    admin: AdminUser,
    conn: asyncpg.Connection = Depends(get_conn),
):
    from pg_database import get_user_by_username
    from routing import set_manual_busy

    user = await get_user_by_username(conn, username)
    if user is None or user["role"] != "nurse":
        raise HTTPException(status_code=404, detail=f"'{username}' is not a nurse")
    set_manual_busy(username, body.busy)
    logger.info("Admin %s set nurse %s manual busy=%s", admin["username"], username, body.busy)
    return {"username": username, "busy": body.busy}


# ── Admin routing-attribute editors ───────────────────────────────────────────

class StatusBody(BaseModel):
    status: str = Field(..., description="available | in_patient_room | on_break | off_duty")

class CompetenciesBody(BaseModel):
    competencies: str = Field("", description="Comma-separated skills, e.g. 'icu_certified,iv_start'")

class SupervisorBody(BaseModel):
    is_supervisor: bool

class AcuityBody(BaseModel):
    acuity: int | None = Field(None, ge=1, le=5, description="ESI 1 (most acute) – 5")


@router.post("/nurses/{username}/status", summary="Admin: set a nurse's live status")
async def admin_set_nurse_status(
    username: str, body: StatusBody, admin: AdminUser,
    conn: asyncpg.Connection = Depends(get_conn),
):
    from pg_database import set_nurse_status
    if not await set_nurse_status(conn, username, body.status):
        raise HTTPException(404, f"Nurse '{username}' not found or invalid status")
    logger.info("Admin %s set %s status=%s", admin["username"], username, body.status)
    return {"username": username, "status": body.status}


@router.post("/nurses/{username}/competencies", summary="Admin: set a nurse's competencies")
async def admin_set_nurse_competencies(
    username: str, body: CompetenciesBody, admin: AdminUser,
    conn: asyncpg.Connection = Depends(get_conn),
):
    from pg_database import set_nurse_competencies
    if not await set_nurse_competencies(conn, username, body.competencies):
        raise HTTPException(404, f"Nurse '{username}' not found")
    return {"username": username, "competencies": body.competencies}


@router.post("/nurses/{username}/supervisor", summary="Admin: set/clear supervisor flag")
async def admin_set_nurse_supervisor(
    username: str, body: SupervisorBody, admin: AdminUser,
    conn: asyncpg.Connection = Depends(get_conn),
):
    from pg_database import set_nurse_supervisor
    if not await set_nurse_supervisor(conn, username, body.is_supervisor):
        raise HTTPException(404, f"Nurse '{username}' not found")
    return {"username": username, "is_supervisor": body.is_supervisor}


@router.post("/rooms/{room_number}/acuity", summary="Admin: set a room patient's ESI acuity")
async def admin_set_room_acuity(
    room_number: str, body: AcuityBody, admin: AdminUser,
    conn: asyncpg.Connection = Depends(get_conn),
):
    from pg_database import set_patient_acuity
    await set_patient_acuity(conn, room_number, body.acuity)
    return {"room_number": room_number, "acuity": body.acuity}


# ── GET /admin/nurses ─────────────────────────────────────────────────────────

@router.get("/nurses", summary="All nurses")
async def admin_nurses(
    _admin: AdminUser,
    conn: asyncpg.Connection = Depends(get_conn),
    db:   aiosqlite.Connection = Depends(get_db),
):
    """Returns every nurse account with their alert response counts."""
    nurses = await get_all_nurses(conn)

    # Build alert-ack counts per nurse from SQLite
    async with db.execute(
        "SELECT ack_by, COUNT(*) AS cnt FROM alerts WHERE ack_by IS NOT NULL GROUP BY ack_by"
    ) as cur:
        ack_rows = await cur.fetchall()
    ack_map = {row[0]: row[1] for row in ack_rows}

    result = []
    for n in nurses:
        result.append({
            "id":           n["id"],
            "username":     n["username"],
            "full_name":    n["full_name"],
            "ward":         n["ward"],
            "is_active":    n["is_active"],
            "created_at":   n["created_at"].isoformat() if n["created_at"] else None,
            "alerts_acked": ack_map.get(n["username"], 0),
        })
    return result


# ── GET /admin/patients ───────────────────────────────────────────────────────

@router.get("/patients", summary="All patients")
async def admin_patients(
    _admin: AdminUser,
    include_discharged: bool = Query(default=True, description="Include discharged patients"),
    conn: asyncpg.Connection = Depends(get_conn),
    db:   aiosqlite.Connection = Depends(get_db),
):
    """Returns every patient with their room, diagnosis, attending nurse, and alert counts."""
    patients = await get_all_patients_admin(conn)

    # Alert counts per room from SQLite
    async with db.execute(
        "SELECT room_id, COUNT(*) AS cnt FROM alerts GROUP BY room_id"
    ) as cur:
        alert_rows = await cur.fetchall()
    alert_map = {row[0]: row[1] for row in alert_rows}

    result = []
    for p in patients:
        if not include_discharged and p["is_discharged"]:
            continue
        result.append({
            "id":            p["id"],
            "username":      p["username"],
            "full_name":     p["full_name"],
            "room_number":   p["room_number"],
            "age":           p["age"],
            "diagnosis":     p["diagnosis"],
            "attending":     p["attending"],
            "is_discharged": p["is_discharged"],
            "admitted_at":   p["admitted_at"].isoformat() if p["admitted_at"] else None,
            "registered_at": p["registered_at"].isoformat() if p["registered_at"] else None,
            "total_alerts":  alert_map.get(p["room_number"], 0),
        })
    return result


# ── GET /admin/rooms ──────────────────────────────────────────────────────────

@router.get("/rooms", summary="Room overview — patient + attending nurse")
async def admin_rooms(
    _admin: AdminUser,
    conn: asyncpg.Connection = Depends(get_conn),
    db:   aiosqlite.Connection = Depends(get_db),
):
    """One row per occupied room: patient name, diagnosis, attending nurse, latest alert."""
    rooms = await get_room_overview(conn)

    # Latest alert per room
    async with db.execute(
        """
        SELECT room_id, priority, intent, transcript, created_at, acknowledged
        FROM alerts
        WHERE id IN (
            SELECT MAX(id) FROM alerts GROUP BY room_id
        )
        """
    ) as cur:
        latest_rows = await cur.fetchall()
    latest_map: dict[str, dict] = {}
    for row in latest_rows:
        latest_map[row[0]] = {
            "priority":     row[1],
            "intent":       row[2],
            "transcript":   row[3],
            "created_at":   row[4],
            "acknowledged": bool(row[5]),
        }

    # Unacked count per room
    async with db.execute(
        "SELECT room_id, COUNT(*) AS cnt FROM alerts WHERE acknowledged=0 GROUP BY room_id"
    ) as cur:
        unacked_rows = await cur.fetchall()
    unacked_map = {row[0]: row[1] for row in unacked_rows}

    result = []
    for r in rooms:
        room_id = r["room_number"]
        result.append({
            "room_number":       room_id,
            "patient_name":      r["patient_name"],
            "diagnosis":         r["diagnosis"],
            "attending_username": r["attending_username"],
            "attending_name":    r["attending_name"],
            "ward":              r["ward"],
            "admitted_at":       r["admitted_at"].isoformat() if r["admitted_at"] else None,
            "unacked_alerts":    unacked_map.get(room_id, 0),
            "latest_alert":      latest_map.get(room_id),
        })
    return result


# ── GET /admin/alerts ─────────────────────────────────────────────────────────

@router.get("/alerts", summary="All alerts with optional filters")
async def admin_alerts(
    _admin: AdminUser,
    db:          aiosqlite.Connection = Depends(get_db),
    limit:       int  = Query(default=100, ge=1, le=500),
    offset:      int  = Query(default=0,   ge=0),
    room_id:     str  = Query(default="",  description="Filter by room, e.g. 4B"),
    priority:    str  = Query(default="",  description="Filter by priority: Critical|Urgent|Routine"),
    unacked_only: bool = Query(default=False),
):
    """Paginated alert list for admin. Supports filtering by room, priority, ack state."""
    conditions: list[str] = []
    params:     list[Any] = []

    if room_id:
        conditions.append(f"room_id = ?")
        params.append(room_id)
    if priority:
        conditions.append(f"priority = ?")
        params.append(priority)
    if unacked_only:
        conditions.append("acknowledged = 0")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    # Total count for pagination
    async with db.execute(f"SELECT COUNT(*) FROM alerts {where}", params) as cur:
        total = (await cur.fetchone())[0]

    # Actual rows (distress_score intentionally omitted — removed from product)
    params_paged = params + [limit, offset]
    async with db.execute(
        f"""
        SELECT id, room_id, priority, intent, transcript,
               acknowledged, ack_by, created_at, ack_at, wav_path
        FROM alerts
        {where}
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
        """,
        params_paged,
    ) as cur:
        rows = await cur.fetchall()

    alerts = [
        {
            "id":            row[0],
            "room_id":       row[1],
            "priority":      row[2],
            "intent":        row[3],
            "transcript":    row[4],
            "acknowledged":  bool(row[5]),
            "ack_by":        row[6],
            "created_at":    row[7],
            "ack_at":        row[8],
            "wav_path":      row[9],
        }
        for row in rows
    ]

    return {"total": total, "limit": limit, "offset": offset, "alerts": alerts}


# ── GET /admin/alerts/stats ───────────────────────────────────────────────────

@router.get("/alerts/stats", summary="Alert aggregates by priority, room, intent")
async def admin_alert_stats(
    _admin: AdminUser,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Aggregate counts for charts: by priority, by room (top 10), by intent."""

    async def _agg(query: str) -> list[dict]:
        async with db.execute(query) as cur:
            rows = await cur.fetchall()
        return [{"label": row[0], "count": row[1]} for row in rows]

    by_priority = await _agg(
        "SELECT priority, COUNT(*) FROM alerts GROUP BY priority ORDER BY COUNT(*) DESC"
    )
    by_room = await _agg(
        "SELECT room_id, COUNT(*) FROM alerts GROUP BY room_id ORDER BY COUNT(*) DESC LIMIT 10"
    )
    by_intent = await _agg(
        "SELECT intent, COUNT(*) FROM alerts GROUP BY intent ORDER BY COUNT(*) DESC"
    )

    # Alerts per day (last 14 days)
    async with db.execute(
        """
        SELECT DATE(created_at) AS day, COUNT(*) AS cnt
        FROM alerts
        WHERE created_at >= DATE('now', '-14 days')
        GROUP BY day
        ORDER BY day ASC
        """
    ) as cur:
        rows = await cur.fetchall()
    by_day = [{"date": row[0], "count": row[1]} for row in rows]

    # Alerts by hour of day (0-23) — activity heat pattern
    async with db.execute(
        """
        SELECT CAST(strftime('%H', created_at) AS INTEGER) AS hr, COUNT(*)
        FROM alerts
        GROUP BY hr
        ORDER BY hr
        """
    ) as cur:
        rows = await cur.fetchall()
    hour_map = {row[0]: row[1] for row in rows if row[0] is not None}
    by_hour = [{"hour": h, "count": hour_map.get(h, 0)} for h in range(24)]

    # Nurse workload — alerts acknowledged per nurse
    async with db.execute(
        """
        SELECT ack_by, COUNT(*)
        FROM alerts
        WHERE ack_by IS NOT NULL
        GROUP BY ack_by
        ORDER BY COUNT(*) DESC
        LIMIT 10
        """
    ) as cur:
        rows = await cur.fetchall()
    nurse_workload = [{"label": row[0], "count": row[1]} for row in rows]

    # Ack rate
    async with db.execute(
        "SELECT SUM(acknowledged), COUNT(*) FROM alerts"
    ) as cur:
        row = await cur.fetchone()
    acked_total = row[0] or 0
    grand_total = row[1] or 1
    ack_rate = round(acked_total / grand_total * 100, 1)

    return {
        "by_priority":    by_priority,
        "by_room":        by_room,
        "by_intent":      by_intent,
        "by_day":         by_day,
        "by_hour":        by_hour,
        "nurse_workload": nurse_workload,
        "ack_rate":       ack_rate,
    }


# ── GET /admin/logs ───────────────────────────────────────────────────────────

@router.get("/logs", summary="Tail the server log file")
async def admin_logs(
    _admin: AdminUser,
    lines: int = Query(default=200, ge=1, le=2000, description="Number of log lines to return"),
):
    """Return the last N lines from the server log for live monitoring."""
    if not _LOG_FILE.exists():
        return {"lines": [], "file": str(_LOG_FILE), "warning": "Log file not found"}

    try:
        # Efficient tail — read from end of file
        with open(_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        all_lines = content.splitlines()
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return {
            "lines":      tail,
            "total_lines": len(all_lines),
            "file":       str(_LOG_FILE),
        }
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not read log file: {exc}",
        )


# ══════════════════════════════════════════════════════════════════════════════
#  MANAGEMENT ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

# ── Request models ────────────────────────────────────────────────────────────

class ApprovalRequest(BaseModel):
    status: str = Field(..., description="pending | approved | rejected")


class RoomCreateRequest(BaseModel):
    room_number: str = Field(..., min_length=1, max_length=10)
    ward:        str | None = Field(None, max_length=50)
    status:      str = Field(default="unoccupied")
    notes:       str | None = Field(None, max_length=200)


class RoomUpdateRequest(BaseModel):
    status: str | None = Field(None, description="occupied | unoccupied | cleaning | maintenance")
    ward:   str | None = Field(None, max_length=50)
    notes:  str | None = Field(None, max_length=200)


class AssignRequest(BaseModel):
    nurse_username: str | None = Field(None, description="Nurse username, or null to unassign")


class DischargeRequest(BaseModel):
    discharged: bool = Field(..., description="True to discharge, False to re-admit")


_VALID_APPROVAL = {"pending", "approved", "rejected"}
_VALID_ROOM_STATUS = {"occupied", "unoccupied", "cleaning", "maintenance"}


# ── Nurse approvals ───────────────────────────────────────────────────────────

@router.get("/nurse-approvals", summary="List nurses by approval status")
async def list_nurse_approvals(
    _admin: AdminUser,
    status_filter: str = Query(default="", alias="status",
                               description="Filter: pending | approved | rejected"),
    conn: asyncpg.Connection = Depends(get_conn),
):
    """List nurses, optionally filtered by approval status (default: all)."""
    rows = await get_nurses_by_status(conn, status_filter or None)
    return [
        {
            "id":              r["id"],
            "username":        r["username"],
            "full_name":       r["full_name"],
            "ward":            r["ward"],
            "is_active":       r["is_active"],
            "approval_status": r["approval_status"],
            "created_at":      r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]


@router.post("/nurse-approvals/{user_id}", summary="Approve / reject a nurse")
async def update_nurse_approval(
    user_id: int,
    body: ApprovalRequest,
    _admin: AdminUser,
    conn: asyncpg.Connection = Depends(get_conn),
):
    """Set a nurse's approval status. Valid: pending, approved, rejected."""
    if body.status not in _VALID_APPROVAL:
        raise HTTPException(status_code=422, detail=f"Invalid status. Use one of {_VALID_APPROVAL}")
    row = await set_nurse_approval(conn, user_id, body.status)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Nurse {user_id} not found")
    logger.info("Nurse %s approval set to '%s' by admin", row["username"], body.status)
    return {
        "id":              row["id"],
        "username":        row["username"],
        "full_name":       row["full_name"],
        "ward":            row["ward"],
        "approval_status": row["approval_status"],
    }


# ── Room management ───────────────────────────────────────────────────────────

@router.get("/rooms-manage", summary="List all rooms with occupancy status")
async def list_rooms_manage(
    _admin: AdminUser,
    conn: asyncpg.Connection = Depends(get_conn),
):
    """Full room list including unoccupied rooms, current patient, attending nurse."""
    rows = await get_all_rooms(conn)
    return [
        {
            "id":                 r["id"],
            "room_number":        r["room_number"],
            "ward":               r["ward"],
            "status":             r["status"],
            "notes":              r["notes"],
            "updated_at":         r["updated_at"].isoformat() if r["updated_at"] else None,
            "patient_name":       r["patient_name"],
            "diagnosis":          r["diagnosis"],
            "attending_username": r["attending_username"],
            "attending_name":     r["attending_name"],
        }
        for r in rows
    ]


@router.post("/rooms-manage", summary="Create a new room", status_code=status.HTTP_201_CREATED)
async def create_room_endpoint(
    body: RoomCreateRequest,
    _admin: AdminUser,
    conn: asyncpg.Connection = Depends(get_conn),
):
    """Create a new room record."""
    if body.status not in _VALID_ROOM_STATUS:
        raise HTTPException(status_code=422, detail=f"Invalid status. Use one of {_VALID_ROOM_STATUS}")
    try:
        row = await create_room(conn, body.room_number.strip(), body.ward, body.status, body.notes)
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail=f"Room {body.room_number} already exists")
    logger.info("Room %s created by admin (status=%s)", body.room_number, body.status)
    return {
        "id":          row["id"],
        "room_number": row["room_number"],
        "ward":        row["ward"],
        "status":      row["status"],
        "notes":       row["notes"],
    }


@router.patch("/rooms-manage/{room_number}", summary="Update room status / ward / notes")
async def update_room_endpoint(
    room_number: str,
    body: RoomUpdateRequest,
    _admin: AdminUser,
    conn: asyncpg.Connection = Depends(get_conn),
):
    """Update a room's occupancy status, ward, or notes."""
    if body.status is not None and body.status not in _VALID_ROOM_STATUS:
        raise HTTPException(status_code=422, detail=f"Invalid status. Use one of {_VALID_ROOM_STATUS}")
    row = await update_room(conn, room_number, body.status, body.ward, body.notes)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Room {room_number} not found")
    logger.info("Room %s updated by admin (status=%s)", room_number, row["status"])
    return {
        "id":          row["id"],
        "room_number": row["room_number"],
        "ward":        row["ward"],
        "status":      row["status"],
        "notes":       row["notes"],
        "updated_at":  row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@router.get("/room-status-summary", summary="Room counts by status (for donut chart)")
async def room_status_summary(
    _admin: AdminUser,
    conn: asyncpg.Connection = Depends(get_conn),
):
    rows = await get_room_status_summary(conn)
    return [{"label": r["status"], "count": r["count"]} for r in rows]


# ── Patient assignment ────────────────────────────────────────────────────────

@router.get("/assignable-nurses", summary="Approved active nurses for assignment dropdowns")
async def assignable_nurses(
    _admin: AdminUser,
    conn: asyncpg.Connection = Depends(get_conn),
):
    rows = await get_approved_nurse_usernames(conn)
    return [{"username": r["username"], "full_name": r["full_name"], "ward": r["ward"]} for r in rows]


@router.post("/patients/{patient_id}/assign", summary="Assign a patient to a nurse")
async def assign_patient(
    patient_id: int,
    body: AssignRequest,
    _admin: AdminUser,
    conn: asyncpg.Connection = Depends(get_conn),
):
    """Set (or clear) the attending nurse for a patient."""
    nurse = body.nurse_username.strip() if body.nurse_username else None
    row = await assign_patient_nurse(conn, patient_id, nurse)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Patient {patient_id} not found")
    logger.info("Patient %d assigned to nurse '%s' by admin", patient_id, nurse or "(unassigned)")
    return {
        "id":          row["id"],
        "full_name":   row["full_name"],
        "room_number": row["room_number"],
        "attending":   row["attending"],
    }


@router.post("/patients/{patient_id}/discharge", summary="Discharge / re-admit a patient")
async def discharge_patient(
    patient_id: int,
    body: DischargeRequest,
    _admin: AdminUser,
    conn: asyncpg.Connection = Depends(get_conn),
):
    """Mark a patient discharged or re-admit them."""
    row = await set_patient_discharge(conn, patient_id, body.discharged)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Patient {patient_id} not found")
    logger.info("Patient %d discharge set to %s by admin", patient_id, body.discharged)
    return {
        "id":            row["id"],
        "full_name":     row["full_name"],
        "room_number":   row["room_number"],
        "is_discharged": row["is_discharged"],
    }
