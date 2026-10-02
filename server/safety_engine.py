"""Deterministic safety gate for patient-facing CareVoice responses.

This layer runs before any future generative model. It deliberately refuses to
make medication, diagnosis, or emergency-care decisions autonomously.
"""
from __future__ import annotations

from models import Intent
from clinical_context import answerability_for_intent
from priority_engine import compute_priority, has_critical_marker, has_urgent_marker


def classify_safety(intent: Intent, transcript: str, context: dict) -> dict:
    """Return a deterministic safety decision for one patient request."""
    priority = compute_priority(intent, transcript)

    if has_critical_marker(transcript) or priority.value == "Critical":
        return {
            "decision": "urgent_escalation",
            "safety_level": "emergency",
            "priority": priority.value,
            "reason": "A critical symptom or emergency marker was detected.",
        }

    # Symptoms that are clinically meaningful but not necessarily emergencies
    # must not be answered by a generative model without human review.
    symptom_markers = [
        "dizzy", "dizziness", "vomiting", "vomit", "nausea", "fever",
        "temperature", "rash", "swelling", "confused", "confusion",
        "vision", "blurred", "weak", "weakness", "numb", "numbness",
    ]
    if any(marker in transcript.lower() for marker in symptom_markers):
        return {
            "decision": "human_review",
            "safety_level": "clinical",
            "priority": priority.value,
            "reason": "A clinically significant symptom was detected and requires human assessment.",
        }

    if intent == Intent.MEDICATION:
        return {
            "decision": "human_review",
            "safety_level": "clinical",
            "priority": priority.value,
            "reason": "Medication requests require nurse/doctor confirmation.",
        }

    if intent == Intent.PAIN:
        return {
            "decision": "human_review",
            "safety_level": "clinical",
            "priority": priority.value,
            "reason": "Pain requests require clinical assessment.",
        }

    base = answerability_for_intent(intent.value, context)
    return {
        "decision": base["decision"],
        "safety_level": "clinical" if base["decision"] == "human_review" else "routine",
        "priority": priority.value,
        "reason": base["reason"],
    }
