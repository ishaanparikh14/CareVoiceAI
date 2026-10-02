"""Read-only EMR endpoints used by the nurse dashboard and AI layer."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from auth import CurrentUser, NurseUser, PatientUser
from clinical_context import build_clinical_context
from emr_service import get_patient_by_room, get_patient_emr
from pg_database import get_conn, get_patient_by_user_id

router = APIRouter(prefix="/emr", tags=["EMR"])


@router.get("/me")
async def my_emr(current_user: PatientUser, conn=Depends(get_conn)):
    patient = await get_patient_by_user_id(conn, current_user["user_id"])
    if not patient:
        raise HTTPException(404, "Patient record not found")
    emr = await get_patient_emr(conn, patient["id"])
    return {"clinical_context": build_clinical_context(emr)}


@router.get("/room/{room_number}")
async def emr_for_room(room_number: str, current_user: CurrentUser, conn=Depends(get_conn)):
    if current_user["role"] not in {"nurse", "admin"}:
        raise HTTPException(403, "Nurse or admin role required")
    emr = await get_patient_by_room(conn, room_number)
    if not emr:
        raise HTTPException(404, "Active patient not found for this room")
    if current_user["role"] == "nurse" and emr["patient"]["attending"] not in {None, current_user["username"]}:
        raise HTTPException(403, "Patient is not assigned to this nurse")
    return {"clinical_context": build_clinical_context(emr)}
