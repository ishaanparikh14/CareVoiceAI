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

    Critical : distress_score >= 0.8 OR intent == Emergency
               → Immediate loud alert + TTS on nurse speaker
    Urgent   : distress_score >= 0.55 OR intent in {Pain, Medication}
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
    language:       str   = Field(default="en", description="Detected language code (e.g. 'en', 'hi')")
    nlp_summary:    str   = Field(default="", description="Short nurse-facing summary of the request")
    intent:         Intent = Field(..., description="Classified intent category")
    distress_score: float  = Field(
        default=0.0, ge=0.0, le=1.0, description="Multimodal distress/urgency signal (0..1)"
    )
    priority:       Priority = Field(..., description="Computed priority level")

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
    """Full PostgreSQL-backed alert record."""
    id: int
    room_id: str
    patient_name: str = "Patient"
    language: str = "en"
    nlp_summary: str = ""
    priority: Priority
    initial_priority: Priority
    intent: Intent
    distress_score: float = 0.0
    transcript: str
    wav_path: str | None = None
    acknowledged: bool
    ack_by: str | None = None
    created_at: str
    ack_at: str | None = None
    attended: bool = False
    attended_by: str | None = None
    attended_at: str | None = None
    escalation_deadline: str | None = None
    escalated: bool = False
    escalated_at: str | None = None
    escalation_count: int = 0

    @classmethod
    def from_db_row(cls, row) -> "AlertResponse":
        d = dict(row)
        def iso(v):
            return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v is not None else None)
        return cls(
            id=d["id"], room_id=d["room_id"], patient_name=d.get("patient_name") or "Patient",
            language=d.get("language") or "en",
            nlp_summary=d.get("nlp_summary") or "",
            priority=Priority(d["priority"]),
            initial_priority=Priority(d.get("initial_priority") or d["priority"]),
            intent=Intent(d["intent"]), distress_score=float(d.get("distress_score") or 0),
            transcript=d["transcript"], wav_path=d.get("wav_path"),
            acknowledged=bool(d.get("acknowledged")), ack_by=d.get("ack_by"),
            created_at=iso(d.get("created_at")), ack_at=iso(d.get("ack_at")),
            attended=bool(d.get("attended")), attended_by=d.get("attended_by"),
            attended_at=iso(d.get("attended_at")),
            escalation_deadline=iso(d.get("escalation_deadline")),
            escalated=bool(d.get("escalated_at")), escalated_at=iso(d.get("escalated_at")),
            escalation_count=int(d.get("escalation_count") or 0),
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


# ── Attendance ────────────────────────────────────────────────────────────────
# Separate from acknowledgement: ACK only means a nurse has seen the alert.
# ATTEND means the patient has actually been cared for, and is the only thing
# that stops Urgent→Critical auto-escalation.

class AttendRequest(BaseModel):
    """Request body for POST /alerts/{id}/attend."""
    attended_by: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Identifier of the nurse who attended to the patient (e.g. 'Nurse-A')",
    )


class AttendResponse(BaseModel):
    """Response body for POST /alerts/{id}/attend."""
    alert_id:    int
    attended:    bool = False
    attended_by: str = ""
    attended_at: str = Field(..., description="ISO-8601 UTC time")
    message:     str = Field(default="Alert marked attended")


# ── WebSocket push payload ────────────────────────────────────────────────────

class WsAlertPayload(BaseModel):
    """
    JSON payload broadcast over the /ws/nurse WebSocket whenever a new alert
    is created.  Nurse clients parse this to update their dashboard in real time.

    Intentionally a subset of AlertResponse — nurses need the essentials fast;
    they can fetch the full record via GET /alerts/{id} if needed.
    """
    event:          Literal["new_alert"] = "new_alert"
    alert_id:       int
    room_id:        str
    patient_name:   str = "Patient"
    language:       str = "en"
    nlp_summary:    str = ""
    priority:       Priority
    initial_priority: Priority
    escalated:      bool = Field(
        default=False,
        description="True when this push is an Urgent→Critical auto-escalation, not a fresh AI-generated Critical alert",
    )
    escalation_deadline: str | None = Field(
        default=None,
        description="ISO-8601 UTC deadline for Urgent alerts; null once escalated/attended or for non-Urgent alerts",
    )
    attended:       bool = False
    intent:         Intent
    distress_score: float = 0.0   # deprecated; kept for client compat
    transcript:     str
    created_at:     str

    @classmethod
    def from_alert_response(cls, a: AlertResponse) -> "WsAlertPayload":
        return cls(
            alert_id            = a.id,
            room_id             = a.room_id,
            patient_name        = a.patient_name,
            language            = a.language,
            nlp_summary         = getattr(a, "nlp_summary", ""),
            priority            = a.priority,
            initial_priority    = a.initial_priority,
            escalated           = a.escalated,
            escalation_deadline = a.escalation_deadline,
            attended            = a.attended,
            intent              = a.intent,
            distress_score      = a.distress_score,
            transcript          = a.transcript,
            created_at          = a.created_at,
        )

class WsAlertUpdate(BaseModel):
    """
    Full alert-state update broadcast to nurse clients when an existing
    alert changes state, such as ACK, ATTEND, or escalation.
    """
    event: Literal["alert_updated"] = "alert_updated"

    alert_id: int
    room_id: str
    patient_name: str = "Patient"
    language: str = "en"
    nlp_summary: str = ""

    priority: Priority
    initial_priority: Priority

    escalated: bool = False
    escalation_deadline: str | None = None

    acknowledged: bool = False
    ack_by: str | None = None
    ack_at: str | None = None

    attended: bool = False
    attended_by: str | None = None
    attended_at: str | None = None

    intent: Intent
    distress_score: float = 0.0
    transcript: str
    created_at: str

    @classmethod
    def from_alert_response(cls, a: AlertResponse) -> "WsAlertUpdate":
        return cls(
            alert_id=a.id,
            room_id=a.room_id,
            patient_name=a.patient_name,
            language=a.language,
            nlp_summary=getattr(a, "nlp_summary", ""),
            priority=a.priority,
            initial_priority=a.initial_priority,
            escalated=a.escalated,
            escalation_deadline=a.escalation_deadline,
            acknowledged=a.acknowledged,
            ack_by=a.ack_by,
            ack_at=a.ack_at,
            attended=a.attended,
            attended_by=a.attended_by,
            attended_at=a.attended_at,
            intent=a.intent,
            distress_score=a.distress_score,
            transcript=a.transcript,
            created_at=a.created_at,
        )


# ── Health check ──────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status:     Literal["ok"] = "ok"
    version:    str = "0.1.0"
    use_stub:   bool
    db_path:    str
