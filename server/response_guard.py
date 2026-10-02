"""Deterministic validator for LLM-generated patient responses.

The validator is deliberately conservative. The LLM can never expand the set
of clinically/hospital-verified options supplied in the context. Any failed
validation causes the caller to retain its deterministic fallback response.
"""
from __future__ import annotations

import re


def _normalise(value: str) -> str:
    return " ".join(str(value).strip().lower().split())


def _allergy_names(context: dict) -> set[str]:
    names: set[str] = set()
    for item in context.get("allergies", []) or []:
        if isinstance(item, dict):
            for key in ("allergen", "name", "allergy"):
                value = item.get(key)
                if value:
                    names.add(_normalise(value))
        elif item:
            names.add(_normalise(item))
    return names


def _active_diet_tags(context: dict) -> set[str]:
    tags: set[str] = set()

    orders = (
        context.get("active_diet_orders")
        or context.get("diet_orders")
        or []
    )

    for order in orders:
        if not isinstance(order, dict):
            continue

        text = " ".join(
            str(order.get(k, ""))
            for k in ("diet_type", "restrictions", "instructions")
        ).lower()

        if "diabet" in text:
            tags.add("diabetic")
        if "regular" in text:
            tags.add("regular")
        if "renal" in text or "kidney" in text:
            tags.add("renal")
        if "cardiac" in text or "heart" in text:
            tags.add("cardiac")
        if "low sodium" in text or "low-sodium" in text:
            tags.add("low_sodium")
        if "liquid" in text:
            tags.add("liquid")
        if "soft" in text:
            tags.add("soft")

    return tags


def _verified_food_options(context: dict) -> dict[str, str]:
    """Return normalized available option name -> canonical display name.

    An option is considered verified only when:
      1. it is explicitly available;
      2. it is compatible with the active diet, when diet_tags are supplied;
      3. it does not contain an explicitly documented allergen;
      4. the patient is not under an NPO restriction.

    If an option has no diet_tags, we do NOT infer that it is safe for a
    restricted diet. It is rejected for patients with an active restrictive
    diet because absence of evidence is not evidence of compatibility.
    """
    if context.get("npo") is True:
        return {}

    active_tags = _active_diet_tags(context)
    allergy_names = _allergy_names(context)

    raw_options = context.get("available_food_options") or []
    verified: dict[str, str] = {}

    for item in raw_options:
        if not isinstance(item, dict):
            continue

        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue

        if item.get("available", True) is not True:
            continue

        item_tags = {
            _normalise(x)
            for x in (item.get("diet_tags") or [])
            if str(x).strip()
        }

        item_allergens = {
            _normalise(x)
            for x in (item.get("allergens") or [])
            if str(x).strip()
        }

        # If the patient has a restrictive diet, the option must explicitly
        # carry at least one compatible diet tag.
        restrictive = bool(active_tags) and active_tags != {"regular"}
        if restrictive and not (active_tags & item_tags):
            continue

        # Explicit allergy overlap always rejects the option.
        if allergy_names & item_allergens:
            continue

        normalized_name = _normalise(name)
        verified[normalized_name] = name.strip()

    return verified


def validate_llm_response(
    candidate: dict,
    intent: str,
    context: dict,
) -> tuple[bool, str]:
    if not isinstance(candidate, dict):
        return False, "candidate_not_object"

    response = candidate.get("response")
    referenced_options = candidate.get("referenced_options")

    if not isinstance(response, str) or not response.strip():
        return False, "empty_response"

    response = response.strip()

    # Keep the patient-facing response concise.
    if len(response) > 600:
        return False, "response_too_long"

    if not isinstance(referenced_options, list):
        return False, "invalid_referenced_options"

    if not all(isinstance(x, str) for x in referenced_options):
        return False, "invalid_referenced_option_type"

    # The LLM must never claim an external action that the backend did not
    # actually execute.
    forbidden_action_claims = [
        r"\bi (have )?(checked|contacted|called|alerted|notified)\b",
        r"\bi(?:'ve| have) (requested|placed|ordered)\b",
        r"\bthe (nurse|doctor|kitchen|hospital) (has|have|will)\b",
        r"\border (has been|was) placed\b",
        r"\bthe doctor approved\b",
    ]

    lowered = response.lower()

    for pattern in forbidden_action_claims:
        if re.search(pattern, lowered):
            return False, "unsupported_action_claim"

    if intent == "Food/Water":
        verified = _verified_food_options(context)

        # If no food option is independently verified, an LLM response must
        # not be allowed to invent/recommend one.
        if not verified:
            return False, "no_verified_food_options"

        referenced = {
            _normalise(x)
            for x in referenced_options
            if x.strip()
        }

        # Every option named by the LLM must be explicitly verified.
        if not referenced.issubset(set(verified)):
            return False, "unverified_or_diet_incompatible_food_option"

        if not referenced:
            return False, "food_response_has_no_verified_option"

        # Every referenced option must actually appear in the patient-facing
        # response. This prevents hidden structured claims from diverging from
        # what the patient hears.
        for option in referenced:
            if option not in lowered:
                return False, "food_option_missing_from_response"

    else:
        # LLM must not provide medication dosing/change instructions even for
        # requests that happened to reach a safe/general LLM path.
        medication_patterns = [
            r"\btake\s+\w+.*\b(?:mg|milligram|tablet|pill)\b",
            r"\b(?:start|stop|change|increase|decrease)\s+(?:your\s+)?(?:medication|medicine|dose)\b",
        ]

        if any(re.search(pattern, lowered) for pattern in medication_patterns):
            return False, "medication_advice"

    return True, "validated"
