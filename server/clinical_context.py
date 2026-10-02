"""Build a compact, authoritative clinical context for AI decisions."""
from __future__ import annotations


def build_clinical_context(emr: dict) -> dict:
    """Return only context useful to a patient-facing decision engine.

    Important safety rule: absent information is represented as unknown/empty;
    it must never be interpreted as a negative clinical finding.
    """
    patient = emr["patient"]
    active_diet = [x for x in emr["diet_orders"] if x.get("status", "active") == "active"]
    active_meds = [x for x in emr["medications"] if x.get("status", "active") == "active"]
    active_restrictions = emr["restrictions"]

    npo = any(
        "npo" in f"{x.get('restriction_type','')} {x.get('restriction_value','')}".lower()
        for x in active_restrictions
    ) or any("npo" in f"{x.get('diet_type','')} {x.get('restrictions','')}".lower() for x in active_diet)

    return {
        "patient": {
            "id": patient["id"],
            "name": patient["full_name"],
            "age": patient["age"],
            "room": patient["room_number"],
            "diagnosis": patient["diagnosis"],
            "attending": patient["attending"],
        },
        "conditions": emr["conditions"],
        "allergies": emr["allergies"],
        "active_medications": active_meds,
        "active_diet_orders": active_diet,
        "restrictions": active_restrictions,
        "npo": npo,
        "doctor_instructions": emr["doctor_instructions"],
        "recent_nursing_notes": emr["nursing_notes"],
        "recent_vitals": emr["recent_vitals"],
        "available_food_options": emr.get("available_food_options", []),
    }


def answerability_for_intent(intent: str, context: dict) -> dict:
    """Safety pre-check before an LLM is allowed to answer.

    This is intentionally conservative. It does not diagnose or prescribe.
    """
    normalized = intent.strip().lower()

    if normalized in {"medication", "pain"}:
        return {"decision": "human_review", "reason": "Medication/pain requests require clinical review."}

    if normalized in {"emergency", "symptom"}:
        return {"decision": "urgent_escalation", "reason": "Potentially clinical or emergency symptom."}

    if normalized in {"food/water", "food", "nutrition"}:
        if context["npo"]:
            return {"decision": "human_review", "reason": "Patient has an NPO restriction."}
        if not context["active_diet_orders"]:
            return {"decision": "human_review", "reason": "No active diet order is available."}
        return {"decision": "context_answer", "reason": "Diet context is available."}

    if normalized in {"mobility"}:
        if context["restrictions"]:
            return {"decision": "human_review", "reason": "Mobility restrictions must be respected."}
        return {"decision": "context_answer", "reason": "No explicit mobility restriction is recorded."}

    return {"decision": "safe_general", "reason": "No high-risk clinical action detected."}
