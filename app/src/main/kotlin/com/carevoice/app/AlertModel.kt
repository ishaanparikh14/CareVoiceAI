package com.carevoice.app

/**
 * Client-side representation of a single alert.
 * Maps to AlertResponse (GET /alerts/latest, GET /alerts/{id}) and to
 * WsAlertPayload (the /ws/nurse push) in the backend's models.py — the two
 * payloads share every field below, just under a different id key
 * ("id" vs "alert_id"), which NurseActivity's alertFromJson() handles.
 */
data class AlertModel(
    val id:                 Int,
    val roomId:             String,
    val patientName:        String = "Patient",
    val nlpSummary:         String = "",
    val priority:           String,   // CURRENT priority: "Critical" | "Urgent" | "Routine"
    val initialPriority:    String,   // priority at creation time, before any escalation
    val intent:             String,
    val distressScore:      Float,
    val transcript:         String,
    val createdAt:          String,   // ISO-8601 UTC — formatted for display in the adapter
    val acknowledged:       Boolean,
    val ackedBy:            String?,
    val language: String = "en",
    // ── Escalation / attendance (added for Urgent→Critical auto-escalation) ──
    val attended:           Boolean = false,
    val attendedBy:         String? = null,
    val attendedAt:         String? = null,
    // Server-authoritative deadline for the 5-minute Urgent→Critical timer.
    // Android NEVER computes "now + 5 minutes" itself — it only displays
    // this value, which the backend sets at alert creation time.
    val escalationDeadline: String? = null,
    // True only when this alert was auto-escalated from Urgent to Critical
    // by the backend (distinct from an AI-generated Critical alert, which
    // never carries this flag and never had an Urgent timer).
    val escalated:          Boolean = false,
    val patientAcknowledged: Boolean = false,
    val patientAckAt:         String? = null
)
