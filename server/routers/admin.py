"""Admin endpoints backed entirely by PostgreSQL."""
import logging
import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query
from auth import AdminUser
from config import settings
from pg_database import (
    get_conn, get_all_nurses, get_all_patients_admin, get_admin_stats,
    get_room_overview, get_nurses_by_status, set_nurse_approval, get_all_rooms,
    create_room, update_room, assign_patient_nurse, set_patient_discharge,
    get_approved_nurse_usernames, get_room_status_summary,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["Admin"])
_LOG_FILE = settings.BASE_DIR / "server.log"


def iso(v): return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v is not None else None)


@router.get("/stats")
async def admin_stats(_admin: AdminUser, conn: asyncpg.Connection = Depends(get_conn)):
    base = await get_admin_stats(conn)
    total, unacked, critical = await conn.fetchrow(
        "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE acknowledged=FALSE) AS unacked, COUNT(*) FILTER (WHERE priority='Critical') AS critical FROM alerts"
    )
    return {**base, "total_alerts": total, "unacked_alerts": unacked, "critical_alerts": critical}


@router.get("/nurses")
async def admin_nurses(_admin: AdminUser, conn: asyncpg.Connection = Depends(get_conn)):
    nurses = await get_all_nurses(conn)
    rows = await conn.fetch("SELECT ack_by, COUNT(*) AS cnt FROM alerts WHERE ack_by IS NOT NULL GROUP BY ack_by")
    ack_map = {r["ack_by"]: r["cnt"] for r in rows}
    return [{"id":n["id"],"username":n["username"],"full_name":n["full_name"],"ward":n["ward"],"is_active":n["is_active"],"created_at":iso(n["created_at"]),"alerts_acked":ack_map.get(n["username"],0)} for n in nurses]


@router.get("/patients")
async def admin_patients(_admin: AdminUser, include_discharged: bool = True, conn: asyncpg.Connection = Depends(get_conn)):
    patients = await get_all_patients_admin(conn)
    counts = await conn.fetch("SELECT room_id, COUNT(*) AS cnt FROM alerts GROUP BY room_id")
    cmap = {r["room_id"]: r["cnt"] for r in counts}
    out=[]
    for p in patients:
        if not include_discharged and p["is_discharged"]: continue
        out.append({"id":p["id"],"username":p["username"],"full_name":p["full_name"],"room_number":p["room_number"],"age":p["age"],"diagnosis":p["diagnosis"],"attending":p["attending"],"is_discharged":p["is_discharged"],"admitted_at":iso(p["admitted_at"]),"registered_at":iso(p.get("registered_at")),"total_alerts":cmap.get(p["room_number"],0)})
    return out


@router.get("/rooms")
async def admin_rooms(_admin: AdminUser, conn: asyncpg.Connection = Depends(get_conn)):
    rooms = await get_room_overview(conn)
    latest = await conn.fetch("""SELECT DISTINCT ON (room_id) room_id, priority, intent, transcript, created_at, acknowledged FROM alerts ORDER BY room_id, created_at DESC""")
    pending = await conn.fetch("SELECT room_id, COUNT(*) AS cnt FROM alerts WHERE acknowledged=FALSE GROUP BY room_id")
    latest_map={r["room_id"]:{"priority":r["priority"],"intent":r["intent"],"transcript":r["transcript"],"created_at":iso(r["created_at"]),"acknowledged":bool(r["acknowledged"])} for r in latest}
    pmap={r["room_id"]:r["cnt"] for r in pending}
    return [{"room_number":r["room_number"],"patient_name":r["patient_name"],"diagnosis":r["diagnosis"],"attending_username":r["attending_username"],"attending_name":r["attending_name"],"ward":r["ward"],"admitted_at":iso(r["admitted_at"]),"unacked_alerts":pmap.get(r["room_number"],0),"latest_alert":latest_map.get(r["room_number"])} for r in rooms]


@router.get("/alerts")
async def admin_alerts(_admin: AdminUser, limit:int=Query(100,ge=1,le=500), offset:int=Query(0,ge=0), room_id:str="", priority:str="", unacked_only:bool=False, conn:asyncpg.Connection=Depends(get_conn)):
    conditions=[]; params=[]
    if room_id: params.append(room_id); conditions.append(f"room_id=${len(params)}")
    if priority: params.append(priority); conditions.append(f"priority=${len(params)}")
    if unacked_only: conditions.append("acknowledged=FALSE")
    where=" WHERE "+" AND ".join(conditions) if conditions else ""
    total=await conn.fetchval("SELECT COUNT(*) FROM alerts"+where,*params)
    params2=params+[limit,offset]
    rows=await conn.fetch("SELECT * FROM alerts"+where+f" ORDER BY created_at DESC LIMIT ${len(params2)-1} OFFSET ${len(params2)}",*params2)
    return {"total":total,"limit":limit,"offset":offset,"alerts":[dict(r) for r in rows]}


@router.get("/alerts/stats")
async def admin_alert_stats(_admin: AdminUser, conn:asyncpg.Connection=Depends(get_conn)):
    async def agg(column):
        return [{"label":r[0],"count":r[1]} for r in await conn.fetch(f"SELECT {column}, COUNT(*) FROM alerts GROUP BY {column} ORDER BY COUNT(*) DESC")]
    by_priority=await agg("priority"); by_room=await conn.fetch("SELECT room_id,COUNT(*) FROM alerts GROUP BY room_id ORDER BY COUNT(*) DESC LIMIT 10"); by_intent=await agg("intent")
    by_room=[{"label":r[0],"count":r[1]} for r in by_room]
    by_day=[{"date":r[0].isoformat(),"count":r[1]} for r in await conn.fetch("SELECT created_at::date,COUNT(*) FROM alerts WHERE created_at>=NOW()-INTERVAL '14 days' GROUP BY 1 ORDER BY 1")]
    hours=await conn.fetch("SELECT EXTRACT(HOUR FROM created_at)::int,COUNT(*) FROM alerts GROUP BY 1 ORDER BY 1")
    hmap={r[0]:r[1] for r in hours}; by_hour=[{"hour":h,"count":hmap.get(h,0)} for h in range(24)]
    nw=[{"label":r[0],"count":r[1]} for r in await conn.fetch("SELECT ack_by,COUNT(*) FROM alerts WHERE ack_by IS NOT NULL GROUP BY ack_by ORDER BY COUNT(*) DESC LIMIT 10")]
    acked,total=await conn.fetchrow("SELECT COUNT(*) FILTER(WHERE acknowledged),COUNT(*) FROM alerts")
    return {"by_priority":by_priority,"by_room":by_room,"by_intent":by_intent,"by_day":by_day,"by_hour":by_hour,"nurse_workload":nw,"ack_rate":round(acked/(total or 1)*100,1)}


@router.get("/logs")
async def admin_logs(_admin:AdminUser, lines:int=Query(200,ge=1,le=2000)):
    if not _LOG_FILE.exists(): return {"lines":[],"file":str(_LOG_FILE),"warning":"Log file not found"}
    content=_LOG_FILE.read_text(encoding="utf-8",errors="replace").splitlines()
    return {"lines":content[-lines:],"total_lines":len(content),"file":str(_LOG_FILE)}

from pydantic import BaseModel, Field

class ApprovalRequest(BaseModel):
    status: str
class RoomCreateRequest(BaseModel):
    room_number: str = Field(..., min_length=1, max_length=10)
    ward: str | None = None
    status: str = "unoccupied"
    notes: str | None = None
class RoomUpdateRequest(BaseModel):
    status: str | None = None
    ward: str | None = None
    notes: str | None = None
class AssignRequest(BaseModel):
    nurse_username: str | None = None
class DischargeRequest(BaseModel):
    discharged: bool
_VALID_APPROVAL={"pending","approved","rejected"}
_VALID_ROOM_STATUS={"occupied","unoccupied","cleaning","maintenance"}

@router.get("/nurse-approvals")
async def list_nurse_approvals(_admin:AdminUser,status_filter:str=Query("",alias="status"),conn:asyncpg.Connection=Depends(get_conn)):
    rows=await get_nurses_by_status(conn,status_filter or None)
    return [{"id":r["id"],"username":r["username"],"full_name":r["full_name"],"ward":r["ward"],"is_active":r["is_active"],"approval_status":r["approval_status"],"created_at":iso(r["created_at"])} for r in rows]

@router.post("/nurse-approvals/{user_id}")
async def update_nurse_approval(user_id:int,body:ApprovalRequest,_admin:AdminUser,conn:asyncpg.Connection=Depends(get_conn)):
    if body.status not in _VALID_APPROVAL: raise HTTPException(422,f"Invalid status. Use one of {_VALID_APPROVAL}")
    row=await set_nurse_approval(conn,user_id,body.status)
    if row is None: raise HTTPException(404,f"Nurse {user_id} not found")
    return {"id":row["id"],"username":row["username"],"full_name":row["full_name"],"ward":row["ward"],"approval_status":row["approval_status"]}

@router.get("/rooms-manage")
async def list_rooms_manage(_admin:AdminUser,conn:asyncpg.Connection=Depends(get_conn)):
    rows=await get_all_rooms(conn)
    return [{"id":r["id"],"room_number":r["room_number"],"ward":r["ward"],"status":r["status"],"notes":r["notes"],"updated_at":iso(r["updated_at"]),"patient_name":r["patient_name"],"diagnosis":r["diagnosis"],"attending_username":r["attending_username"],"attending_name":r["attending_name"]} for r in rows]

@router.post("/rooms-manage",status_code=201)
async def create_room_endpoint(body:RoomCreateRequest,_admin:AdminUser,conn:asyncpg.Connection=Depends(get_conn)):
    if body.status not in _VALID_ROOM_STATUS: raise HTTPException(422,f"Invalid status. Use one of {_VALID_ROOM_STATUS}")
    try: row=await create_room(conn,body.room_number.strip(),body.ward,body.status,body.notes)
    except asyncpg.UniqueViolationError: raise HTTPException(409,f"Room {body.room_number} already exists")
    return dict(row)

@router.patch("/rooms-manage/{room_number}")
async def update_room_endpoint(room_number:str,body:RoomUpdateRequest,_admin:AdminUser,conn:asyncpg.Connection=Depends(get_conn)):
    if body.status is not None and body.status not in _VALID_ROOM_STATUS: raise HTTPException(422,f"Invalid status. Use one of {_VALID_ROOM_STATUS}")
    row=await update_room(conn,room_number,body.status,body.ward,body.notes)
    if row is None: raise HTTPException(404,f"Room {room_number} not found")
    return dict(row)

@router.get("/room-status-summary")
async def room_status_summary(_admin:AdminUser,conn:asyncpg.Connection=Depends(get_conn)):
    return [{"label":r["status"],"count":r["count"]} for r in await get_room_status_summary(conn)]

@router.get("/assignable-nurses")
async def assignable_nurses(_admin:AdminUser,conn:asyncpg.Connection=Depends(get_conn)):
    return [{"username":r["username"],"full_name":r["full_name"],"ward":r["ward"]} for r in await get_approved_nurse_usernames(conn)]

@router.post("/patients/{patient_id}/assign")
async def assign_patient(patient_id:int,body:AssignRequest,_admin:AdminUser,conn:asyncpg.Connection=Depends(get_conn)):
    row=await assign_patient_nurse(conn,patient_id,body.nurse_username.strip() if body.nurse_username else None)
    if row is None: raise HTTPException(404,f"Patient {patient_id} not found")
    return {"id":row["id"],"full_name":row["full_name"],"room_number":row["room_number"],"attending":row["attending"]}

@router.post("/patients/{patient_id}/discharge")
async def discharge_patient(patient_id:int,body:DischargeRequest,_admin:AdminUser,conn:asyncpg.Connection=Depends(get_conn)):
    row=await set_patient_discharge(conn,patient_id,body.discharged)
    if row is None: raise HTTPException(404,f"Patient {patient_id} not found")
    return {"id":row["id"],"full_name":row["full_name"],"room_number":row["room_number"],"is_discharged":row["is_discharged"]}
