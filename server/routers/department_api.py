"""
routers/department_api.py — endpoints for department dashboards + admin control
of the central routing agent.

Department staff (canteen / pharmacy / floor managers):
    GET  /department/requests            — my pending queue
    POST /department/{id}/acknowledge    — I'm handling this
    POST /department/{id}/done           — completed
    POST /department/{id}/not-mine       — bounce back to the nurse (logged override)
    GET  /department/me                  — who am I / which department

Nurse:
    POST /department/reroute             — nurse manually reroutes an alert to a dept

Admin:
    GET   /admin/department/config       — current feature-flag state
    PATCH /admin/department/config       — toggle routing/shadow/per-department
    GET   /admin/department/requests     — all department requests (audit)

All routes are JWT-protected via the role guards in auth.py.
"""

import logging

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from auth import AdminUser, CurrentUser, DepartmentUser, department_of
from database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Department Routing"])


# ── Department-staff: my queue ────────────────────────────────────────────────

@router.get("/department/me", summary="Current department-staff identity")
async def department_me(user: DepartmentUser):
    return {
        "username":   user["username"],
        "full_name":  user["full_name"],
        "role":       user["role"],
        "department": department_of(user),
    }


@router.get("/department/requests", summary="My department's pending requests")
async def my_department_requests(
    user: DepartmentUser,
    include_done: bool = Query(default=False),
    db: aiosqlite.Connection = Depends(get_db),
):
    from database import list_department_requests_for_user
    rows = await list_department_requests_for_user(
        db, user["username"], include_done=include_done
    )
    return {"department": department_of(user), "requests": rows}


@router.post("/department/{request_id}/acknowledge", summary="Acknowledge a request")
async def acknowledge_request(
    request_id: int,
    user: DepartmentUser,
    db: aiosqlite.Connection = Depends(get_db),
):
    from department_dispatch import acknowledge
    res = await acknowledge(db, request_id, user["username"])
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("error", "could not acknowledge"))
    return res


@router.post("/department/{request_id}/done", summary="Mark a request completed")
async def complete_request(
    request_id: int,
    user: DepartmentUser,
    db: aiosqlite.Connection = Depends(get_db),
):
    from department_dispatch import complete
    res = await complete(db, request_id, user["username"])
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("error", "could not complete"))
    return res


class NotMineRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


@router.post("/department/{request_id}/not-mine", summary="Bounce a request back to the nurse")
async def not_mine(
    request_id: int,
    body: NotMineRequest,
    user: DepartmentUser,
    db: aiosqlite.Connection = Depends(get_db),
):
    from department_dispatch import reject
    res = await reject(db, request_id, user["username"], body.reason)
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("error", "could not reject"))
    return res


# ── Nurse: manual reroute to a department ─────────────────────────────────────

class RerouteRequest(BaseModel):
    department: str = Field(..., description="canteen | pharmacy | housekeeping")


@router.post("/department/reroute", summary="Nurse manually reroutes an alert to a department")
async def nurse_reroute(
    alert_id: int = Query(..., description="Alert to reroute"),
    body: RerouteRequest = ...,
    current_user: CurrentUser = ...,
    db: aiosqlite.Connection = Depends(get_db),
):
    if current_user["role"] not in ("nurse", "admin"):
        raise HTTPException(status_code=403, detail="Nurse or admin role required")
    dep = body.department.strip().lower()
    if dep not in ("canteen", "pharmacy", "housekeeping"):
        raise HTTPException(status_code=422, detail="Unknown department")

    from database import get_alert_by_id
    alert = await get_alert_by_id(db, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")

    # Logged as a manual override (free training data for Phase 2).
    logger.info("[DEPT][OVERRIDE] nurse %s rerouted alert=%d → %s transcript=%r",
                current_user["username"], alert_id, dep, alert["transcript"])

    from department_dispatch import route_request
    from department_router import MODEL_VERSION
    res = await route_request(
        db, alert_id=alert_id, department=dep, room_id=alert["room_id"],
        transcript=alert["transcript"], predicted_department=dep,
        routing_reason="manual_nurse_reroute", confidence=1.0, model_version=MODEL_VERSION,
    )
    return res


# ── Admin: feature-flag control ───────────────────────────────────────────────

class DeptConfigPatch(BaseModel):
    enabled:      bool | None = None
    shadow:       bool | None = None
    canteen:      bool | None = None
    pharmacy:     bool | None = None
    housekeeping: bool | None = None


@router.get("/admin/department/config", summary="Department routing feature flags")
async def get_department_config(
    _admin: AdminUser,
    db: aiosqlite.Connection = Depends(get_db),
):
    from department_dispatch import resolve_flags
    return await resolve_flags(db)


@router.patch("/admin/department/config", summary="Toggle department routing flags")
async def patch_department_config(
    body: DeptConfigPatch,
    admin: AdminUser,
    db: aiosqlite.Connection = Depends(get_db),
):
    from database import set_department_flag
    from department_dispatch import resolve_flags
    changes = body.model_dump(exclude_none=True)
    for key, val in changes.items():
        await set_department_flag(db, key, bool(val))
    logger.info("Admin %s updated department flags: %s", admin["username"], changes)
    return await resolve_flags(db)


@router.get("/admin/department/requests", summary="All department requests (audit)")
async def admin_department_requests(
    _admin: AdminUser,
    limit: int = Query(default=100, ge=1, le=500),
    db: aiosqlite.Connection = Depends(get_db),
):
    async with db.execute(
        """
        SELECT * FROM department_requests
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    ) as cur:
        rows = await cur.fetchall()
    return {"requests": [dict(r) for r in rows]}
