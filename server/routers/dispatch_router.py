"""
routers/dispatch_router.py — nurse + admin endpoints for the Uber-style dispatch loop.

Nurse (phone app):
    POST /dispatch/{request_id}/accept   — claim a request (atomic single-winner)
    POST /dispatch/{request_id}/decline  — decline → rebroadcast to free nurses
    POST /dispatch/status                — set own live status (available/busy)

Admin (dispatch board):
    GET  /dispatch/available-nurses      — nurses that are AVAILABLE right now
    POST /dispatch/{request_id}/reassign — force a request onto an available nurse

The admin board also reads GET /admin/dispatch-board (routers/admin.py) which now
includes the active dispatch requests and their state.
"""

import logging

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from auth import CurrentUser
from database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dispatch", tags=["Dispatch"])


class DeclineRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=200,
                               description="Why the nurse is declining (e.g. 'engaged with another patient')")


class StatusRequest(BaseModel):
    available: bool = Field(..., description="True = I'm available for new requests, False = busy")


class ReassignRequest(BaseModel):
    nurse_username: str = Field(..., min_length=1, description="Nurse to receive the request (must be available)")


def _require_nurse(user: dict) -> None:
    if user["role"] != "nurse":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Nurse role required")


def _require_admin(user: dict) -> None:
    if user["role"] != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")


# ── Nurse: accept ─────────────────────────────────────────────────────────────

@router.post("/{request_id}/accept", summary="Nurse accepts a dispatch request")
async def accept_request(
    request_id: int,
    current_user: CurrentUser,
    db: aiosqlite.Connection = Depends(get_db),
):
    _require_nurse(current_user)
    from dispatch import accept
    res = await accept(db, request_id, current_user["username"])
    if res.get("ok"):
        return res
    err = res.get("error")
    if err == "not_found":
        raise HTTPException(status_code=404, detail="Request not found")
    if err == "already_taken":
        raise HTTPException(status_code=409,
                            detail=f"Request already taken by {res.get('taken_by') or 'another nurse'}")
    if err in ("already_resolved", "already_handled"):
        raise HTTPException(status_code=409, detail="Request already handled")
    if err == "not_available":
        raise HTTPException(status_code=409, detail="You are not available — set yourself free first")
    raise HTTPException(status_code=409, detail=err or "Could not accept")


# ── Nurse: decline ────────────────────────────────────────────────────────────

@router.post("/{request_id}/decline", summary="Nurse declines → rebroadcast to free nurses")
async def decline_request(
    request_id: int,
    body: DeclineRequest,
    current_user: CurrentUser,
    db: aiosqlite.Connection = Depends(get_db),
):
    _require_nurse(current_user)
    from dispatch import decline
    res = await decline(db, request_id, current_user["username"], body.reason)
    if res.get("ok"):
        return res
    err = res.get("error")
    if err == "not_found":
        raise HTTPException(status_code=404, detail="Request not found")
    if err == "not_your_request":
        raise HTTPException(status_code=409, detail="This request is no longer assigned to you")
    if err == "alert_gone":
        raise HTTPException(status_code=409, detail="The underlying request was already handled")
    raise HTTPException(status_code=409, detail=err or "Could not decline")


# ── Nurse: set own status ─────────────────────────────────────────────────────

@router.post("/status", summary="Nurse sets own availability")
async def set_status(
    body: StatusRequest,
    current_user: CurrentUser,
    db: aiosqlite.Connection = Depends(get_db),
):
    _require_nurse(current_user)
    from routing import set_manual_busy
    # available == NOT manually busy. (Presence/offline is handled by the socket.)
    set_manual_busy(current_user["username"], not body.available)
    logger.info("Nurse %s set available=%s", current_user["username"], body.available)
    return {"username": current_user["username"], "available": body.available}


# ── Admin: available nurses ────────────────────────────────────────────────────

@router.get("/available-nurses", summary="Nurses that are available right now (for reassign)")
async def list_available_nurses(
    current_user: CurrentUser,
    db: aiosqlite.Connection = Depends(get_db),
):
    _require_admin(current_user)
    from dispatch import available_nurses
    from pg_database import get_conn, get_nurse_routing_map
    free = await available_nurses(db)
    async for conn in get_conn():
        routing_map = await get_nurse_routing_map(conn)
        break
    return [
        {"username": u, "full_name": routing_map.get(u, {}).get("full_name", u),
         "ward": routing_map.get(u, {}).get("ward")}
        for u in free
    ]


# ── Admin: reassign ────────────────────────────────────────────────────────────

@router.post("/{request_id}/reassign", summary="Admin reassigns a request to an available nurse")
async def reassign_request(
    request_id: int,
    body: ReassignRequest,
    current_user: CurrentUser,
    db: aiosqlite.Connection = Depends(get_db),
):
    _require_admin(current_user)
    from dispatch import admin_reassign
    res = await admin_reassign(db, request_id, body.nurse_username.strip())
    if res.get("ok"):
        return res
    err = res.get("error")
    if err == "not_found":
        raise HTTPException(status_code=404, detail="Request not found")
    if err == "nurse_unavailable":
        raise HTTPException(status_code=409,
                            detail=f"{body.nurse_username} is offline — they must be logged in to receive the request")
    if err in ("already_resolved", "alert_gone"):
        raise HTTPException(status_code=409, detail="Request already handled")
    raise HTTPException(status_code=409, detail=err or "Could not reassign")
