"""
routers/auth_router.py — authentication endpoints.

POST /auth/login
    Accepts JSON { username, password } OR form-encoded (for Swagger UI compat).
    Returns { access_token, token_type, role, full_name, room_number? }.

GET  /auth/me
    Returns the current user's profile from the JWT + DB.
    Patients also get their room_number from the patients table.

GET  /auth/patients   (nurse only)
    Returns all non-discharged patients — used by nurse dashboard to show the
    ward list.
"""

import logging

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field, field_validator

from auth import (
    CurrentUser,
    NurseUser,
    authenticate_user,
    create_access_token,
    get_current_user,
    hash_password,
)
from config import settings
from pg_database import (
    create_nurse,
    create_patient,
    get_all_patients,
    get_patients_for_nurse,
    get_conn,
    get_patient_by_user_id,
    update_patient_profile,
    update_user_profile,
    username_exists,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Auth"])


# ── Pydantic schemas (local — auth-specific, not in global models.py) ─────────

class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type:   str = "bearer"
    role:         str
    full_name:    str
    user_id:      int
    room_number:  str | None = None   # populated for patients only
    ward:         str | None = None   # populated for nurses only


class UserProfile(BaseModel):
    user_id:     int
    username:    str
    full_name:   str
    role:        str
    ward:        str | None = None
    room_number: str | None = None
    # Patient extras
    age:         int | None = None
    diagnosis:   str | None = None
    attending:   str | None = None


class PatientSummary(BaseModel):
    id:          int
    room_number: str
    full_name:   str
    age:         int | None
    diagnosis:   str | None
    attending:   str | None
    username:    str


# ── Registration schemas ──────────────────────────────────────────────────────

class RegisterPatientRequest(BaseModel):
    username:    str   = Field(..., min_length=3,  max_length=40)
    password:    str   = Field(..., min_length=6,  max_length=128)
    full_name:   str   = Field(..., min_length=2,  max_length=100)
    room_number: str   = Field(..., min_length=1,  max_length=10)
    age:         int   | None = Field(None, ge=0, le=150)
    diagnosis:   str   | None = Field(None, max_length=200)
    attending:   str   | None = Field(None, max_length=100)

    @field_validator("username")
    @classmethod
    def username_alphanum(cls, v: str) -> str:
        if not v.replace("_", "").replace(".", "").isalnum():
            raise ValueError("Username may only contain letters, digits, underscores, and dots")
        return v.lower()


class RegisterNurseRequest(BaseModel):
    username:   str = Field(..., min_length=3,  max_length=40)
    password:   str = Field(..., min_length=6,  max_length=128)
    full_name:  str = Field(..., min_length=2,  max_length=100)
    ward:       str = Field(..., min_length=1,  max_length=50)
    admin_key:  str = Field(..., description="Server admin key to authorise nurse registration")

    @field_validator("username")
    @classmethod
    def username_alphanum(cls, v: str) -> str:
        if not v.replace("_", "").replace(".", "").isalnum():
            raise ValueError("Username may only contain letters, digits, underscores, and dots")
        return v.lower()


class RegisterResponse(BaseModel):
    user_id:   int
    username:  str
    role:      str
    full_name: str
    message:   str = "Registration successful"


class UpdateProfileRequest(BaseModel):
    """Fields the user may update. All optional — only supplied fields are changed."""
    full_name:   str | None = Field(None, min_length=2, max_length=100)
    # Patient only
    room_number: str | None = Field(None, min_length=1, max_length=10)
    age:         int | None = Field(None, ge=0, le=150)
    diagnosis:   str | None = Field(None, max_length=200)
    attending:   str | None = Field(None, max_length=100)
    # Nurse only
    ward:        str | None = Field(None, min_length=1, max_length=50)


# ── POST /auth/login ──────────────────────────────────────────────────────────

@router.post(
    "/login",
    response_model = TokenResponse,
    summary        = "Log in as nurse or patient",
    description    = (
        "Supply username + password. "
        "Returns a signed JWT valid for one shift (8 hours). "
        "Patients receive their room_number in the response so the app can "
        "pre-fill it without a second request."
    ),
)
async def login(
    form: OAuth2PasswordRequestForm = Depends(),
    conn: asyncpg.Connection        = Depends(get_conn),
):
    """
    Accepts OAuth2 form-encoded body (username + password) so the Swagger UI
    'Authorize' button works out of the box.
    """
    user = await authenticate_user(conn, form.username, form.password)
    if user is None:
        # Generic message — don't reveal whether user exists
        raise HTTPException(
            status_code = status.HTTP_401_UNAUTHORIZED,
            detail      = "Incorrect username or password",
            headers     = {"WWW-Authenticate": "Bearer"},
        )

    # Nurses must be approved by an admin before they can log in.
    if user["role"] == "nurse":
        approval = user.get("approval_status", "approved")
        if approval == "pending":
            raise HTTPException(
                status_code = status.HTTP_403_FORBIDDEN,
                detail      = "Your nurse account is awaiting administrator approval.",
            )
        if approval == "rejected":
            raise HTTPException(
                status_code = status.HTTP_403_FORBIDDEN,
                detail      = "Your nurse account registration was declined. Contact an administrator.",
            )

    token = create_access_token({
        "sub":       user["username"],
        "user_id":   user["id"],
        "role":      user["role"],
        "full_name": user["full_name"],
    })

    # For patients, fetch room_number so the UI can show/use it immediately.
    room_number = None
    if user["role"] == "patient":
        patient_row = await get_patient_by_user_id(conn, user["id"])
        if patient_row:
            room_number = patient_row["room_number"]

    logger.info("Login: %s (%s)", user["username"], user["role"])

    return TokenResponse(
        access_token = token,
        role         = user["role"],
        full_name    = user["full_name"],
        user_id      = user["id"],
        room_number  = room_number,
        ward         = user.get("ward"),
    )


# ── GET /auth/me ──────────────────────────────────────────────────────────────

@router.get(
    "/me",
    response_model = UserProfile,
    summary        = "Get current user profile",
)
async def me(
    current_user: CurrentUser,
    conn: asyncpg.Connection = Depends(get_conn),
):
    profile = UserProfile(
        user_id   = current_user["user_id"],
        username  = current_user["username"],
        full_name = current_user["full_name"],
        role      = current_user["role"],
        ward      = current_user.get("ward"),
    )

    if current_user["role"] == "patient":
        patient = await get_patient_by_user_id(conn, current_user["user_id"])
        if patient:
            profile.room_number = patient["room_number"]
            profile.age         = patient["age"]
            profile.diagnosis   = patient["diagnosis"]
            profile.attending   = patient["attending"]

    return profile


# ── PATCH /auth/me ────────────────────────────────────────────────────────────

@router.patch(
    "/me",
    response_model = UserProfile,
    summary        = "Update current user profile",
    description    = "Update full_name and role-specific fields. Only supplied fields are changed.",
)
async def update_me(
    body:         UpdateProfileRequest,
    current_user: CurrentUser,
    conn:         asyncpg.Connection = Depends(get_conn),
):
    user_id = current_user["user_id"]
    role    = current_user["role"]

    # Always update users table (full_name, ward for nurses)
    await update_user_profile(
        conn      = conn,
        user_id   = user_id,
        full_name = body.full_name,
        ward      = body.ward if role == "nurse" else None,
    )

    # Patients: also update the patients table
    if role == "patient":
        await update_patient_profile(
            conn        = conn,
            user_id     = user_id,
            full_name   = body.full_name,
            room_number = body.room_number,
            age         = body.age,
            diagnosis   = body.diagnosis,
            attending   = body.attending,
        )

    # Return the updated profile by re-fetching
    from pg_database import get_user_by_username
    updated_user = await get_user_by_username(conn, current_user["username"])
    profile = UserProfile(
        user_id   = updated_user["id"],
        username  = updated_user["username"],
        full_name = updated_user["full_name"],
        role      = updated_user["role"],
        ward      = updated_user.get("ward"),
    )
    if role == "patient":
        patient = await get_patient_by_user_id(conn, user_id)
        if patient:
            profile.room_number = patient["room_number"]
            profile.age         = patient["age"]
            profile.diagnosis   = patient["diagnosis"]
            profile.attending   = patient["attending"]

    logger.info("Profile updated: %s", current_user["username"])
    return profile


# ── POST /auth/register/patient ───────────────────────────────────────────────

@router.post(
    "/register/patient",
    response_model = RegisterResponse,
    status_code    = status.HTTP_201_CREATED,
    summary        = "Register a new patient account",
)
async def register_patient(
    body: RegisterPatientRequest,
    conn: asyncpg.Connection = Depends(get_conn),
):
    if await username_exists(conn, body.username):
        raise HTTPException(
            status_code = status.HTTP_409_CONFLICT,
            detail      = f"Username '{body.username}' is already taken",
        )

    pw_hash = hash_password(body.password)
    user_id = await create_patient(
        conn          = conn,
        username      = body.username,
        password_hash = pw_hash,
        full_name     = body.full_name,
        room_number   = body.room_number,
        age           = body.age,
        diagnosis     = body.diagnosis,
        attending     = body.attending,
    )
    logger.info("New patient registered: %s → Room %s", body.username, body.room_number)
    return RegisterResponse(
        user_id   = user_id,
        username  = body.username,
        role      = "patient",
        full_name = body.full_name,
    )


# ── POST /auth/register/nurse ─────────────────────────────────────────────────

@router.post(
    "/register/nurse",
    response_model = RegisterResponse,
    status_code    = status.HTTP_201_CREATED,
    summary        = "Register a new nurse account (requires admin key)",
)
async def register_nurse(
    body: RegisterNurseRequest,
    conn: asyncpg.Connection = Depends(get_conn),
):
    # Validate admin key
    if body.admin_key != settings.NURSE_ADMIN_KEY:
        raise HTTPException(
            status_code = status.HTTP_403_FORBIDDEN,
            detail      = "Invalid admin key",
        )

    if await username_exists(conn, body.username):
        raise HTTPException(
            status_code = status.HTTP_409_CONFLICT,
            detail      = f"Username '{body.username}' is already taken",
        )

    pw_hash = hash_password(body.password)
    user_id = await create_nurse(
        conn          = conn,
        username      = body.username,
        password_hash = pw_hash,
        full_name     = body.full_name,
        ward          = body.ward,
    )
    logger.info("New nurse registered (pending approval): %s (Ward %s)", body.username, body.ward)
    return RegisterResponse(
        user_id   = user_id,
        username  = body.username,
        role      = "nurse",
        full_name = body.full_name,
        message   = "Registration submitted. An administrator must approve your account before you can log in.",
    )


# ── GET /auth/patients  (nurse only) ─────────────────────────────────────────

@router.get(
    "/patients",
    response_model = list[PatientSummary],
    summary        = "List ward patients assigned to the current nurse (nurse only)",
)
async def list_patients(
    current_nurse: NurseUser,                # 403 if caller is not a nurse
    all: bool = False,                       # ?all=true → every ward patient
    conn: asyncpg.Connection = Depends(get_conn),
):
    """
    By default returns only the patients whose `attending` == the logged-in
    nurse's username. Pass ?all=true to get the full ward list.
    """
    if all:
        rows = await get_all_patients(conn)
    else:
        rows = await get_patients_for_nurse(conn, current_nurse["username"])
    return [
        PatientSummary(
            id          = r["id"],
            room_number = r["room_number"],
            full_name   = r["full_name"],
            age         = r["age"],
            diagnosis   = r["diagnosis"],
            attending   = r["attending"],
            username    = r["username"],
        )
        for r in rows
    ]
