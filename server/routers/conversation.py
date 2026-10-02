"""Patient conversation endpoints.

The first version is deterministic and safety-gated. It is intentionally
separate from the generative AI layer so we can validate behaviour first.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import CurrentUser, PatientUser
from conversation_engine import process_patient_request
from pg_database import get_conn, get_patient_by_user_id
from emr_service import get_patient_by_room

router = APIRouter(prefix="/conversation", tags=["Patient Conversation"])


class PatientConversationRequest(BaseModel):
    transcript: str = Field(min_length=1, max_length=1000)


class ConversationTestRequest(BaseModel):
    room_number: str = Field(min_length=1, max_length=20)
    transcript: str = Field(min_length=1, max_length=1000)


@router.post("/me")
async def patient_conversation(
    body: PatientConversationRequest,
    current_user: PatientUser,
    conn=Depends(get_conn),
):
    patient = await get_patient_by_user_id(conn, current_user["user_id"])
    if not patient:
        raise HTTPException(404, "Patient record not found")
    try:
        return await process_patient_request(conn, patient["id"], body.transcript)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.post("/test")
async def nurse_conversation_test(
    body: ConversationTestRequest,
    current_user: CurrentUser,
    conn=Depends(get_conn),
):
    """Development-only deterministic test endpoint for nurse/admin users."""
    if current_user["role"] not in {"nurse", "admin"}:
        raise HTTPException(403, "Nurse or admin role required")

    emr = await get_patient_by_room(conn, body.room_number)
    if not emr:
        raise HTTPException(404, "Active patient not found for this room")
    if current_user["role"] == "nurse" and emr["patient"]["attending"] not in {None, current_user["username"]}:
        raise HTTPException(403, "Patient is not assigned to this nurse")

    return await process_patient_request(conn, emr["patient"]["id"], body.transcript)
