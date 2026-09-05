"""
auth.py — password hashing, JWT creation/verification, and FastAPI dependencies.

Flow
----
1.  POST /auth/login  → verify password → issue signed JWT
2.  Every protected route adds  Depends(get_current_user)
3.  Role-restricted routes additionally add  Depends(require_nurse)
                                          or  Depends(require_patient)

JWT payload
-----------
{
  "sub":       "nurse_anna",        # username
  "user_id":   1,                   # users.id
  "role":      "nurse",             # "nurse" | "patient"
  "full_name": "Anna Reyes RN",
  "exp":       <unix timestamp>
}
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated

import asyncpg
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext

from config import settings
from pg_database import get_conn, get_user_by_username

logger = logging.getLogger(__name__)

# ── Passlib context ───────────────────────────────────────────────────────────
# Use argon2 only - bcrypt has compatibility issues on Windows
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


# ── JWT ───────────────────────────────────────────────────────────────────────
# tokenUrl points to the login endpoint so Swagger's "Authorize" button works.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def create_access_token(data: dict) -> str:
    """
    Sign a JWT with the configured secret and algorithm.
    Adds an 'exp' claim based on ACCESS_TOKEN_EXPIRE_MINUTES.
    """
    payload = data.copy()
    expire  = datetime.now(timezone.utc) + timedelta(
        minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    payload["exp"] = expire
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """
    Decode and verify a JWT.  Raises HTTPException 401 on any failure
    (expired, tampered, wrong key, malformed).
    """
    credentials_exc = HTTPException(
        status_code = status.HTTP_401_UNAUTHORIZED,
        detail      = "Invalid or expired token — please log in again",
        headers     = {"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
        if payload.get("sub") is None:
            raise credentials_exc
        return payload
    except JWTError:
        raise credentials_exc


# ── FastAPI dependencies ──────────────────────────────────────────────────────

async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    conn:  Annotated[asyncpg.Connection, Depends(get_conn)],
) -> dict:
    """
    Dependency that decodes the Bearer JWT and verifies the user still exists
    and is active in the database.

    Returns a plain dict with keys:
        user_id, username, full_name, role, ward, is_active
    """
    payload  = decode_token(token)
    username = payload.get("sub")

    row = await get_user_by_username(conn, username)
    if row is None:
        raise HTTPException(
            status_code = status.HTTP_401_UNAUTHORIZED,
            detail      = "User not found or deactivated",
            headers     = {"WWW-Authenticate": "Bearer"},
        )

    return {
        "user_id":   row["id"],
        "username":  row["username"],
        "full_name": row["full_name"],
        "role":      row["role"],
        "ward":      row["ward"],
        "is_active": row["is_active"],
    }


# Typed alias for cleaner route signatures
CurrentUser = Annotated[dict, Depends(get_current_user)]


def require_nurse(current_user: CurrentUser) -> dict:
    """
    Role guard — raises 403 if the authenticated user is not a nurse.
    Use as a second Depends() after get_current_user, or as a standalone dependency.
    """
    if current_user["role"] != "nurse":
        raise HTTPException(
            status_code = status.HTTP_403_FORBIDDEN,
            detail      = "Nurse role required for this endpoint",
        )
    return current_user


def require_patient(current_user: CurrentUser) -> dict:
    """Role guard — raises 403 if the authenticated user is not a patient."""
    if current_user["role"] != "patient":
        raise HTTPException(
            status_code = status.HTTP_403_FORBIDDEN,
            detail      = "Patient role required for this endpoint",
        )
    return current_user


def require_admin(current_user: CurrentUser) -> dict:
    """Role guard — raises 403 if the authenticated user is not an admin."""
    if current_user["role"] != "admin":
        raise HTTPException(
            status_code = status.HTTP_403_FORBIDDEN,
            detail      = "Admin role required for this endpoint",
        )
    return current_user


# Typed aliases for role-guarded routes
NurseUser   = Annotated[dict, Depends(require_nurse)]
PatientUser = Annotated[dict, Depends(require_patient)]
AdminUser   = Annotated[dict, Depends(require_admin)]


# ── Login helper (used by auth router) ───────────────────────────────────────

async def authenticate_user(
    conn:     asyncpg.Connection,
    username: str,
    password: str,
) -> dict | None:
    """
    Verify username + password against the PostgreSQL users table.
    Returns the user dict on success, None on failure.
    Never reveals whether the username exists (constant-time comparison via bcrypt).
    """
    row = await get_user_by_username(conn, username)
    if row is None:
        # Run a dummy hash to prevent timing attacks revealing non-existent users.
        pwd_context.dummy_verify()
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    return dict(row)
