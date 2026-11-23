# -*- coding: utf-8 -*-
"""
diet_rules.py — server-side diet enforcement for canteen orders (pure, testable).

A patient has one diet_restriction. Menu items carry the dietary tags they
SATISFY (veg_only, jain, gluten_free, peanut_allergy_safe, green_veg, ...).

  * Hard diets (veg_only, jain, gluten_free, peanut_allergy): an item without the
    matching tag is blocked ("diet_violation"), whoever is ordering.
  * needs_green_veg: items with green vegetables are RECOMMENDED; others allowed.
  * Clinical diets (npo, dysphagia): ordering is blocked until a dietician has
    recorded a food clearance ("clearance_required").
  * diabetic, low_sodium: allowed items are the dietician's allowed list when a
    plan exists; otherwise items are allowed but flagged for review.
  * A dietician plan (when 'dietician augments' is on) can additionally forbid
    items (avoid list) or restrict to an allowed list.
"""

from __future__ import annotations

HARD_TAG = {
    "veg_only": "veg_only",
    "jain": "jain",
    "gluten_free": "gluten_free",
    "peanut_allergy": "peanut_allergy_safe",
}
RECOMMEND_TAG = {"needs_green_veg": "green_veg", "green_veg": "green_veg"}
CLEARANCE_DIETS = {"npo", "dysphagia"}
REVIEW_DIETS = {"diabetic", "low_sodium"}

DIET_LABELS = {
    "none": "No restriction",
    "veg_only": "Vegetarian only",
    "jain": "Jain",
    "gluten_free": "Gluten free",
    "peanut_allergy": "Peanut allergy",
    "needs_green_veg": "Needs green vegetables",
    "npo": "Nothing by mouth (NPO)",
    "dysphagia": "Dysphagia (soft/modified texture)",
    "diabetic": "Diabetic",
    "low_sodium": "Low sodium",
}


def _tags(item: dict) -> set[str]:
    raw = item.get("tags")
    if isinstance(raw, (list, tuple, set)):
        return {str(t).strip() for t in raw if str(t).strip()}
    return {t.strip() for t in str(raw or "").split(",") if t.strip()}


def _names(values) -> set[str]:
    if not values:
        return set()
    if isinstance(values, str):
        values = values.split(",")
    return {str(v).strip().lower() for v in values if str(v).strip()}


def check_item(item: dict, diet: str | None, plan: dict | None = None) -> dict:
    """Return {"allowed": bool, "recommended": bool, "code": str, "message": str}."""
    diet = (diet or "none").strip().lower()
    name = str(item.get("name", "")).strip().lower()
    tags = _tags(item)

    if diet in CLEARANCE_DIETS and not (plan or {}).get("food_cleared"):
        return {"allowed": False, "recommended": False, "code": "clearance_required",
                "message": f"{DIET_LABELS.get(diet, diet)}: a dietician must clear food first."}

    if plan:
        if name in _names(plan.get("avoid_items")):
            return {"allowed": False, "recommended": False, "code": "diet_violation",
                    "message": "The dietician has advised against this item."}
        allowed = _names(plan.get("allowed_items"))
        if allowed and name not in allowed:
            return {"allowed": False, "recommended": False, "code": "diet_violation",
                    "message": "Not on the dietician's allowed list for this patient."}

    need = HARD_TAG.get(diet)
    if need and need not in tags:
        return {"allowed": False, "recommended": False, "code": "diet_violation",
                "message": f"Not suitable for: {DIET_LABELS.get(diet, diet)}."}

    rec = True
    if diet in RECOMMEND_TAG:
        rec = RECOMMEND_TAG[diet] in tags
    if plan and _names(plan.get("allowed_items")):
        rec = name in _names(plan.get("allowed_items"))
    msg = "Fits the patient's diet." if rec else "Allowed."
    if diet in REVIEW_DIETS and not plan:
        msg = f"{DIET_LABELS[diet]}: allowed, review portion with the dietician."
    return {"allowed": True, "recommended": rec, "code": "ok", "message": msg}


def annotate_menu(menu: list[dict], diet: str | None, plan: dict | None = None) -> list[dict]:
    """Return the menu with allowed / recommended / reason per item."""
    out = []
    for m in menu:
        v = check_item(m, diet, plan)
        out.append({**m, "allowed": v["allowed"], "recommended": v["allowed"] and v["recommended"],
                    "reason": v["message"], "code": v["code"]})
    out.sort(key=lambda x: (not x["recommended"], not x["allowed"], x.get("id", 0)))
    return out
