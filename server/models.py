"""
models.py — Pydantic schemas for every request body, response body, and
internal data-transfer object used across the CareVoice AI server.

Keeping all schemas in one file makes the API contract easy to review and
avoids circular imports between routers.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


# ── Enumerations ──────────────────────────────────────────────────────────────

class Priority(str, Enum):
    """
    Three-level priority system produced by the Priority Engine (Layer 4).

    Critical : distress_score > 0.8  OR  intent == Emergency
               → Immediate loud alert + TTS on nurse speaker
    Urgent   : distress_score > 0.5  OR  intent in {Pain, Medication}
               → Push notification + dashboard banner
    Routine  : everything else
               → Silent dashboard entry only
    """
    CRITICAL = "Critical"
    URGENT   = "Urgent"
    ROUTINE  = "Routine"


class Intent(str, Enum):
    """Nine medical intent categories classified by the pipeline."""
    EMERGENCY        = "Emergency"
    PAIN             = "Pain"
    MEDICATION       = "Medication"
    FOOD_WATER       = "Food/Water"
    MOBILITY         = "Mobility"
    HYGIENE          = "Hygiene"
    EMOTIONAL_SUPPORT = "Emotional Support"
    INFORMATION      = "Information"
    OTHER            = "Other"


# ── Pipeline internal DTOs ────────────────────────────────────────────────────

class PipelineResult(BaseModel):
    """
    Internal data-transfer object passed from pipeline_stub.py (and later the
    real Layer 3 pipeline) to the audio router.  Never serialised directly to
    the client — AlertResponse is the public-facing representation.
    """
    transcript:     str   = Field(..., description="STT output text")
    intent:         Intent = Field(..., description="Classified intent category")
    distress_score: float  = Field(
        default=0.0, ge=0.0, le=1.0, description="Deprecated — no longer used; kept for schema compat"
    )
    priority:       Priority = Field(..., description="Computed priority level")
    language:       str | None = Field(default=None, description="Detected/forced language: en|hi|kn")

    # When False the audio was classified as non-medical chatter and should NOT
    # be forwarded to nurses as an alert (still stored for audit purposes).
    should_alert: bool = Field(default=True, description="False for general non-medical speech")

    # Stub flag — False once real models are plugged in
    is_stub: bool = Field(default=True, description="True when produced by pipeline_stub")


# ── Audio ingest ──────────────────────────────────────────────────────────────

class IngestResponse(BaseModel):
    """
    Response body for POST /audio/ingest (HTTP 201).
    Returned to the Android app immediately after the alert is stored.
    """
    alert_id:       int      = Field(..., description="Database primary key of the new alert")
    room_id:        str      = Field(..., description="Room that submitted the audio")
    priority:       Priority = Field(..., description="Computed priority level")
    intent:         Intent   = Field(..., description="Classified intent")
    distress_score: float    = Field(default=0.0, ge=0.0, le=1.0, description="Deprecated")
    transcript:     str      = Field(..., description="STT transcription")
    is_stub:        bool     = Field(..., description="Whether real models were used")
    # True  → alert was forwarded to nurses
    # False → audio was general chatter, stored for audit but not alerted
    should_alert:   bool     = Field(default=True, description="Whether nurses were notified")
    message:        str      = Field(default="Alert created successfully")


# ── Alert responses ───────────────────────────────────────────────────────────

class AlertResponse(BaseModel):
    """
    Full alert record returned by GET /alerts/latest and GET /alerts/{id}.
    Maps 1-to-1 to the SQLite alerts table row.
    """
    id:             int      = Field(..., description="Alert primary key")
    room_id:        str
    priority:       Priority
    intent:         Intent
    distress_score: float    = Field(default=0.0, ge=0.0, le=1.0, description="Deprecated")
    transcript:     str
    wav_path:       str | None = None
    acknowledged:   bool
    ack_by:         str | None = None
    created_at:     str        = Field(..., description="ISO-8601 UTC creation time")
    ack_at:         str | None = None
    language:       str | None = Field(default=None, description="Detected/forced language: en|hi|kn")
    escalated:      bool       = Field(default=False, description="True if auto-escalated Urgent→Critical")

    @classmethod
    def from_db_row(cls, row: dict) -> "AlertResponse":
        """
        Construct from a raw aiosqlite dict row.
        Converts the integer acknowledged field (0/1) to a Python bool.
        """
        return cls(
            id             = row["id"],
            room_id        = row["room_id"],
            priority       = Priority(row["priority"]),
            intent         = Intent(row["intent"]),
            distress_score = row["distress_score"],
            transcript     = row["transcript"],
            wav_path       = row.get("wav_path"),
            acknowledged   = bool(row["acknowledged"]),
            ack_by         = row.get("ack_by"),
            created_at     = row["created_at"],
            ack_at         = row.get("ack_at"),
            language       = row.get("language"),
            escalated      = bool(row.get("escalated") or 0),
        )


class AlertListResponse(BaseModel):
    """Response body for GET /alerts/latest."""
    alerts:      list[AlertResponse]
    total:       int  = Field(..., description="Number of alerts returned")
    unacked_only: bool = Field(..., description="Whether unacknowledged filter was applied")


# ── Acknowledgement ───────────────────────────────────────────────────────────

class AckRequest(BaseModel):
    """Request body for POST /alerts/{id}/ack."""
    ack_by: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Identifier of the nurse acknowledging the alert (e.g. 'Nurse-A')",
    )


class AckResponse(BaseModel):
    """Response body for POST /alerts/{id}/ack and POST /alerts/manual."""
    alert_id: int
    acknowledged: bool = False
    ack_by:   str = ""
    ack_at:   str  = Field(..., description="ISO-8601 UTC time")
    message:  str  = Field(default="Alert acknowledged")


# ── WebSocket push payload ────────────────────────────────────────────────────

class WsAlertPayload(BaseModel):
    """
    JSON payload broadcast over the /ws/nurse WebSocket whenever a new alert
    is created.  Nurse clients parse this to update their dashboard in real time.

    Intentionally a subset of AlertResponse — nurses need the essentials fast;
    they can fetch the full record via GET /alerts/{id} if needed.
    """
    # "new_alert" for freshly created alerts; "alert_updated" when an existing
    # alert changes server-side (e.g. auto-escalation Urgent→Critical).
    event:          Literal["new_alert", "alert_updated"] = "new_alert"
    alert_id:       int
    room_id:        str
    priority:       Priority
    intent:         Intent
    distress_score: float = 0.0   # deprecated; kept for client compat
    transcript:     str
    created_at:     str
    language:       str | None = None
    escalated:      bool = False

    @classmethod
    def from_alert_response(cls, a: AlertResponse, event: str = "new_alert") -> "WsAlertPayload":
        return cls(
            event          = event,
            alert_id       = a.id,
            room_id        = a.room_id,
            priority       = a.priority,
            intent         = a.intent,
            distress_score = a.distress_score,
            transcript     = a.transcript,
            created_at     = a.created_at,
            language       = a.language,
            escalated      = a.escalated,
        )


# ── Health check ──────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status:     Literal["ok"] = "ok"
    version:    str = "0.1.0"
    use_stub:   bool
    db_path:    str
