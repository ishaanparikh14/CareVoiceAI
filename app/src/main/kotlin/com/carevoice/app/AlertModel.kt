package com.carevoice.app

/**
 * Client-side representation of a single alert from GET /alerts/latest.
 * Maps directly to AlertResponse in models.py.
 */
data class AlertModel(
    val id:            Int,
    val roomId:        String,
    val priority:      String,   // "Critical" | "Urgent" | "Routine"
    val intent:        String,
    val distressScore: Float,
    val transcript:    String,
    val createdAt:     String,   // ISO-8601 UTC — formatted for display in the adapter
    val acknowledged:  Boolean,
    val ackedBy:       String?,
    val escalated:     Boolean = false,  // true once auto-escalated Urgent→Critical
    // ── NLP layer (summary + emotional intelligence) ──────────────────────────
    val patientName:   String? = null,   // resolved patient name for the message prefix
    val summary:       String? = null,   // NLP request summary, e.g. "wants water"
    val emotion:       String? = null    // calm|anxious|distressed|panicked
)
