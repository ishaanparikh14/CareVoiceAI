"""Patient conversation engine.

Deterministic intent + safety decisions happen first. A local LLM is only
allowed to phrase a response after the safety gate approves the request.
Generated text is validated before it can replace the deterministic fallback.
"""
from __future__ import annotations

from models import Intent
from clinical_context import build_clinical_context
from emr_service import get_patient_emr
from safety_engine import classify_safety
from llm_service import generate_patient_response
from response_guard import validate_llm_response


def detect_intent(text: str) -> Intent:
    """Small deterministic classifier used before the production intent model."""
    t = text.lower().strip()

    if any(x in t for x in ["can't breathe", "cannot breathe", "chest pain", "heart attack", "bleeding", "unconscious", "faint"]):
        return Intent.EMERGENCY
    if any(x in t for x in ["pain", "hurts", "hurting", "ache", "dard", " ನೋವು"]):
        return Intent.PAIN
    if any(x in t for x in ["medicine", "medication", "tablet", "pill", "injection", "painkiller"]):
        return Intent.MEDICATION
    if any(x in t for x in ["hungry", "food", "eat", "meal", "breakfast", "lunch", "dinner", "water", "thirsty", "drink", "juice"]):
        return Intent.FOOD_WATER
    if any(x in t for x in ["walk", "get up", "stand", "bathroom", "toilet"]):
        return Intent.MOBILITY
    if any(x in t for x in ["wash", "bath", "clean", "toothbrush"]):
        return Intent.HYGIENE
    if any(x in t for x in ["scared", "afraid", "anxious", "lonely", "worried", "frightened"]):
        return Intent.EMOTIONAL_SUPPORT
    if any(x in t for x in ["doctor", "when", "why", "what", "where", "information"]):
        return Intent.INFORMATION
    return Intent.OTHER


def _food_options(context: dict) -> list[dict]:
    diet_orders = context.get("active_diet_orders", [])
    if not diet_orders or context.get("npo"):
        return []

    tags = set()
    for order in diet_orders:
        diet = str(order.get("diet_type", "")).lower()
        if "diabetic" in diet:
            tags.add("diabetic")
        if "regular" in diet:
            tags.add("regular")

    options = []
    for item in context.get("available_food_options", []):
        if not item.get("available", True):
            continue
        item_tags = {str(x).lower() for x in (item.get("diet_tags") or [])}
        if tags and not (tags & item_tags):
            continue
        allergy_names = {str(a.get("allergen", "")).lower() for a in context.get("allergies", [])}
        item_allergens = {str(x).lower() for x in (item.get("allergens") or [])}
        if allergy_names & item_allergens:
            continue
        options.append(item)
    return options


def _fallback_response(intent: Intent, context: dict, safety: dict) -> dict:
    decision = safety["decision"]
    if decision == "urgent_escalation":
        return {"response": "I’m getting the nurse immediately. Please stay where you are.", "action": "ESCALATE_IMMEDIATELY", "requires_nurse": True}
    if decision == "human_review":
        if intent == Intent.MEDICATION:
            response = "I’ll ask your nurse to confirm that before you take anything."
            action = "REQUEST_NURSE_REVIEW"
        elif intent == Intent.PAIN:
            response = "I’ll alert your nurse so they can assess your pain."
            action = "REQUEST_NURSE_REVIEW"
        elif intent == Intent.FOOD_WATER and context.get("npo"):
            response = "Your current orders restrict food or drinks, so I’ll ask your nurse to confirm what is allowed."
            action = "REQUEST_NURSE_REVIEW"
        else:
            response = "I’ll ask your nurse to help with that."
            action = "REQUEST_NURSE_REVIEW"
        return {"response": response, "action": action, "requires_nurse": True}

    if intent == Intent.FOOD_WATER:
        options = _food_options(context)
        if not options:
            return {"response": "I don’t have a verified food or drink option available right now, so I’ll ask your nurse to help.", "action": "REQUEST_NURSE_REVIEW", "requires_nurse": True}
        names = [x["name"] for x in options[:3]]
        response = "Suitable options available for you are " + ", ".join(names[:-1]) + (f", or {names[-1]}" if len(names) > 1 else names[-1]) + ". Would you like me to request one?"
        return {"response": response, "action": "OFFER_FOOD_OPTION", "requires_nurse": False}

    if intent == Intent.MOBILITY:
        return {"response": "I’ll ask your nurse to assist you because your current care plan includes mobility precautions.", "action": "REQUEST_NURSE_ASSISTANCE", "requires_nurse": True}
    if intent == Intent.HYGIENE:
        return {"response": "I’ll let your nurse know that you need help with that.", "action": "REQUEST_NURSE_ASSISTANCE", "requires_nurse": True}
    if intent == Intent.EMOTIONAL_SUPPORT:
        return {"response": "I’m here with you. I’ll also let your nurse know that you need support.", "action": "REQUEST_NURSE_SUPPORT", "requires_nurse": True}
    return {"response": "I don’t have enough verified information to answer that safely. I’ll ask your nurse to help.", "action": "REQUEST_NURSE_REVIEW", "requires_nurse": True}


async def process_patient_request(conn, patient_id: int, transcript: str, use_llm: bool = True) -> dict:
    emr = await get_patient_emr(conn, patient_id)
    if not emr:
        raise ValueError("Active patient EMR not found")

    context = build_clinical_context(emr)
    intent = detect_intent(transcript)
    safety = classify_safety(intent, transcript, context)
    fallback = _fallback_response(intent, context, safety)

    llm_used = False
    llm_validation = None
    response = fallback["response"]

    # LLM is strictly downstream of the safety gate. It cannot change the
    # decision/action or answer requests marked for clinical review/emergency.
    if use_llm and safety["decision"] in {"context_answer", "safe_general"}:
        try:
            candidate = await generate_patient_response(transcript, intent.value, context)
            valid, reason = validate_llm_response(candidate, intent.value, context)
            llm_validation = reason

            if valid:
                response = candidate["response"]
                llm_used = True
            else:
                # Fail closed: deterministic response remains in place.
                llm_used = False
        except Exception as e:
            print(f"[LLM ERROR] {type(e).__name__}: {e}")
            llm_validation = "llm_error"
            llm_used = False

    return {
        "transcript": transcript,
        "intent": intent.value,
        "safety": safety,
        "response": response,
        "action": fallback["action"],
        "requires_nurse": fallback["requires_nurse"],
        "llm_used": llm_used,
        "llm_validation": llm_validation,
        "patient_context_used": {
            "patient_id": context["patient"]["id"],
            "diagnosis": context["patient"]["diagnosis"],
            "conditions": context["conditions"],
            "allergies": context["allergies"],
            "diet_orders": context["active_diet_orders"],
            "restrictions": context["restrictions"],
        },
    }
