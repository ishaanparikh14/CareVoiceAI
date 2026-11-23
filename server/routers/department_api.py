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
    department: str = Field(..., description="housekeeping")


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
    # Food is ordered through /orders/food; pharmacy is nurse-order only. A
    # nurse can hand a misrouted ground-support task to housekeeping.
    if dep != "housekeeping":
        raise HTTPException(status_code=422, detail="Only housekeeping reroutes are supported")

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
    if res.get("ok"):
        # The nurse handed it off: close it on the nurse side.
        from database import acknowledge_alert
        await acknowledge_alert(db, alert_id, ack_by=current_user["username"])
        await db.execute("UPDATE alerts SET request_class='facility', visible_to_nurse=0 WHERE id=?", (alert_id,))
        await db.commit()
        try:
            from dispatch import on_alert_acknowledged
            await on_alert_acknowledged(db, alert_id, current_user["username"])
        except Exception:
            pass
    else:
        raise HTTPException(status_code=409, detail="No housekeeping staff covers this floor")
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


# ══════════════════════════════════════════════════════════════════════════════
#  ORDER LIFECYCLE ENDPOINTS (food + medicine)
# ══════════════════════════════════════════════════════════════════════════════

# Food orders are placed by the attender or the nurse (see routers/v3_api.py,
# POST /orders/food) with the diet already applied, so the canteen no longer
# "chooses a meal" — it prepares and delivers.

@router.post("/department/order/{request_id}/preparing", summary="Dept marks order preparing")
async def order_preparing(request_id: int, user: DepartmentUser, db: aiosqlite.Connection = Depends(get_db)):
    from department_dispatch import order_mark_preparing
    res = await order_mark_preparing(db, request_id, by=user["username"])
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("error", "could not update"))
    return res


@router.post("/department/order/{request_id}/delivered", summary="Dept marks order delivered")
async def order_delivered(request_id: int, user: DepartmentUser, db: aiosqlite.Connection = Depends(get_db)):
    from department_dispatch import order_mark_delivered
    res = await order_mark_delivered(db, request_id, by=user["username"])
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("error", "could not update"))
    return res


# ── Nurse approval queue + actions ────────────────────────────────────────────

def _require_nurse_or_admin(user: dict) -> None:
    if user["role"] not in ("nurse", "admin"):
        raise HTTPException(status_code=403, detail="Nurse or admin role required")


@router.get("/department/approvals", summary="Orders awaiting nurse approval")
async def approvals_queue(
    current_user: CurrentUser,
    db: aiosqlite.Connection = Depends(get_db),
):
    _require_nurse_or_admin(current_user)
    from database import list_orders_pending_approval
    rows = await list_orders_pending_approval(db)
    return {"approvals": rows}


class RejectBody(BaseModel):
    reason: str | None = None


@router.post("/department/order/{request_id}/approve", summary="Nurse approves an order")
async def order_approve_ep(
    request_id: int,
    current_user: CurrentUser,
    db: aiosqlite.Connection = Depends(get_db),
):
    _require_nurse_or_admin(current_user)
    from department_dispatch import order_approve
    res = await order_approve(db, request_id, by=current_user["username"])
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("error", "could not approve"))
    return res


@router.post("/department/order/{request_id}/reject", summary="Nurse rejects an order")
async def order_reject_ep(
    request_id: int,
    body: RejectBody,
    current_user: CurrentUser,
    db: aiosqlite.Connection = Depends(get_db),
):
    _require_nurse_or_admin(current_user)
    from department_dispatch import order_reject
    res = await order_reject(db, request_id, by=current_user["username"], reason=body.reason)
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("error", "could not reject"))
    return res


# ── Patient medication list (for nurse/pharmacy context) ──────────────────────

@router.get("/department/medications/{room_id}", summary="A patient's medication list")
async def patient_meds(
    room_id: str,
    current_user: CurrentUser,
    db: aiosqlite.Connection = Depends(get_db),
):
    if current_user["role"] not in ("nurse", "admin", "pharmacy"):
        raise HTTPException(status_code=403, detail="Not permitted")
    from pg_database import get_patient_medications
    return {"room_id": room_id, "medications": await get_patient_medications(room_id)}


# ══════════════════════════════════════════════════════════════════════════════
#  NURSE CARE EDITOR — a nurse adjusts diet + medications for HER mapped patients
# ══════════════════════════════════════════════════════════════════════════════

import asyncpg
from pg_database import get_conn

_VALID_DIETS_CARE = {"none", "veg_only", "jain", "gluten_free", "peanut_allergy",
                     "needs_green_veg", "npo", "dysphagia", "diabetic", "low_sodium"}
_VALID_MED_KINDS = {"prescribed", "general"}


async def _assert_nurse_covers(conn, current_user: dict, room_id: str):
    """Authorize: admins may edit anyone; a nurse only her mapped rooms."""
    if current_user["role"] == "admin":
        return
    if current_user["role"] != "nurse":
        raise HTTPException(status_code=403, detail="Nurse or admin role required")
    from pg_database import nurse_covers_room
    if not await nurse_covers_room(conn, current_user["username"], room_id):
        raise HTTPException(status_code=403, detail=f"You are not assigned to room {room_id}")


@router.get("/department/my-care", summary="A patient's OWN diet + prescribed meds (read-only)")
async def my_care(
    current_user: CurrentUser,
    conn: asyncpg.Connection = Depends(get_conn),
):
    """Read-only view for the PATIENT app: their current diet restriction and the
    medications on their chart. Patients cannot edit — diet/meds are clinical
    orders set by staff."""
    if current_user["role"] != "patient":
        raise HTTPException(status_code=403, detail="Patient role required")
    from pg_database import get_patient_by_user_id, get_patient_medications
    p = await get_patient_by_user_id(conn, current_user["user_id"])
    if p is None:
        return {"room_number": None, "full_name": current_user["full_name"],
                "diet_restriction": "none", "medications": []}
    room = p["room_number"]
    meds = await get_patient_medications(room)
    diet = p["diet_restriction"] if "diet_restriction" in p.keys() else "none"
    return {
        "room_number": room, "full_name": p["full_name"],
        "diet_restriction": diet or "none", "medications": meds,
    }


@router.get("/nurse/care/patients", summary="A nurse's mapped patients with diet + meds")
async def nurse_care_patients(
    current_user: CurrentUser,
    conn: asyncpg.Connection = Depends(get_conn),
):
    """List the patients this nurse is responsible for, each with current diet
    and medication lists — the data the nurse-care editor renders."""
    if current_user["role"] not in ("nurse", "admin"):
        raise HTTPException(status_code=403, detail="Nurse or admin role required")
    from pg_database import get_patients_for_nurse, get_all_patients, get_patient_medications
    rows = (await get_all_patients(conn) if current_user["role"] == "admin"
            else await get_patients_for_nurse(conn, current_user["username"]))
    out = []
    for p in rows:
        room = p["room_number"]
        meds = await get_patient_medications(room)
        # diet_restriction isn't in get_patients_for_nurse's SELECT; fetch it.
        diet = await conn.fetchval(
            "SELECT diet_restriction FROM patients WHERE room_number=$1 AND is_discharged=FALSE ORDER BY admitted_at DESC LIMIT 1",
            room,
        )
        out.append({
            "room_number": room, "full_name": p["full_name"],
            "diet_restriction": diet or "none",
            "medications": meds,
        })
    return {"patients": out, "diet_options": sorted(_VALID_DIETS_CARE)}


class NurseDietBody(BaseModel):
    diet_restriction: str = Field(...)


@router.post("/nurse/care/{room_id}/diet", summary="Nurse sets a mapped patient's diet")
async def nurse_set_diet(
    room_id: str,
    body: NurseDietBody,
    current_user: CurrentUser,
    conn: asyncpg.Connection = Depends(get_conn),
):
    diet = body.diet_restriction.strip().lower()
    if diet not in _VALID_DIETS_CARE:
        raise HTTPException(status_code=422, detail=f"Invalid diet. Use one of {sorted(_VALID_DIETS_CARE)}")
    await _assert_nurse_covers(conn, current_user, room_id)
    from pg_database import set_patient_diet_by_room
    await set_patient_diet_by_room(conn, room_id, diet)
    logger.info("Nurse %s set diet for room %s = %s", current_user["username"], room_id, diet)
    return {"room_id": room_id, "diet_restriction": diet}


class NurseMedBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    kind: str = Field(..., description="prescribed | general")
    dosage: str = Field(default="", max_length=120)


@router.post("/nurse/care/{room_id}/medication", summary="Nurse adds/updates a medication")
async def nurse_add_med(
    room_id: str,
    body: NurseMedBody,
    current_user: CurrentUser,
    conn: asyncpg.Connection = Depends(get_conn),
):
    kind = body.kind.strip().lower()
    if kind not in _VALID_MED_KINDS:
        raise HTTPException(status_code=422, detail="kind must be 'prescribed' or 'general'")
    await _assert_nurse_covers(conn, current_user, room_id)
    from pg_database import add_patient_medication
    await add_patient_medication(conn, room_id, body.name.strip(), kind, body.dosage.strip())
    logger.info("Nurse %s set med '%s' (%s) for room %s", current_user["username"], body.name, kind, room_id)
    return {"ok": True}


@router.delete("/nurse/care/{room_id}/medication/{name}", summary="Nurse removes a medication")
async def nurse_remove_med(
    room_id: str,
    name: str,
    current_user: CurrentUser,
    conn: asyncpg.Connection = Depends(get_conn),
):
    await _assert_nurse_covers(conn, current_user, room_id)
    from pg_database import remove_patient_medication
    await remove_patient_medication(conn, room_id, name)
    return {"ok": True}
