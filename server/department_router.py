# -*- coding: utf-8 -*-
"""
department_router.py — the "central routing agent" decision layer.

Runs AFTER intent classification (pipeline.py) and BEFORE the nurse dispatch
(routing.py / dispatch.py). It decides whether a patient request — or each PART
of a multi-part request — should go to the nurse station, canteen, pharmacy or
per-floor housekeeping.

SAFETY-FIRST design (the whole point of this module):
  • Clinical content never leaves the nurse. Medication defaults to the nurse;
    only explicit supply/logistics phrasing ("prescription hasn't arrived",
    "refill") goes to pharmacy. Personal-care hygiene ("soiled bed", "help me
    bathe") is the nurse's; only environmental hygiene ("dirty bathroom",
    "change the bedsheets") is housekeeping.
  • A diet restriction (NPO/dysphagia/diabetic/…) forces Food/Water through the
    nurse instead of straight to the canteen.
  • Any critical/urgent marker (priority_engine) means the NURSE gets the full
    request no matter what else was said.
  • Low STT confidence or an Indic-script auto-correction vetoes every non-nurse
    route — when we aren't sure what was said, a human nurse decides.
  • An asymmetric confidence gate (settings.DEPT_MIN_CONFIDENCE, ~0.85) gates
    every non-nurse route; anything below it falls back to the nurse.

Phase 1 is a keyword + intent-map router: no ML training. Each decision carries
a `routing_reason` ("intent_map" | "keyword" | "model") and a `model_version`
so the interface is ready for a calibrated ML score to drop in later (Phase 2)
without changing any caller. The DepartmentDecision list this returns is what
department_dispatch.py and the shadow-mode logger consume.

This module is PURE: no DB, no network, no async. That keeps it unit-testable in
isolation (see tests/test_department_router.py) with no Postgres or GPU.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from enum import Enum

from models import Intent
import priority_engine

# Bump when the routing rules or keyword lists change materially, so the audit
# trail (alerts.dept_model_version) can tell which logic produced a decision.
MODEL_VERSION = "kw-router-v1"


class Department(str, Enum):
    """The four routing destinations. NURSE is always the safe fallback."""
    NURSE        = "nurse"
    CANTEEN      = "canteen"
    PHARMACY     = "pharmacy"
    HOUSEKEEPING = "housekeeping"


@dataclass
class DepartmentDecision:
    """One routing destination for (part of) a request.

    department   : where this slice goes.
    confidence   : 0..1; for the keyword router this is rule-derived (an explicit
                   keyword hit is high-confidence). A calibrated model score
                   drops in here in Phase 2.
    routing_reason: "intent_map" | "keyword" | "model" — why this was chosen.
    reason       : short human-readable explanation (for the audit log / admin).
    matched      : the keyword fragment / span that triggered a non-nurse route.
    is_primary   : True for the nurse decision that carries the FULL request when
                   a request fans out to several departments. Exactly one primary.
    model_version: the logic/model version that produced this decision.
    """
    department:    Department
    confidence:    float
    routing_reason: str
    reason:        str = ""
    matched:       str | None = None
    is_primary:    bool = False
    model_version: str = MODEL_VERSION


# ── Text normalisation (same approach as priority_engine / intent_keywords) ────

def _normalize(text: str) -> str:
    """Lowercase + strip Unicode combining marks AND apostrophes so Whisper's
    inconsistent diacritics/contractions don't defeat substring matching.
    e.g. "isn't" → "isnt", "साँस" → "सस". Like priority_engine._normalize but
    also collapses apostrophe variants (matching nlp_summary's normaliser)."""
    t = (text or "").lower().strip()
    for ap in ("'", "\u2019", "\u02bc", "`", "\u00b4"):
        t = t.replace(ap, "")
    decomposed = unicodedata.normalize("NFD", t)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) not in ("Mn", "Mc"))
    return unicodedata.normalize("NFC", stripped)


# ── Keyword lists (English, Hindi, Kannada, German), already conceptually ──────
# normalised; we normalise both sides at match time. Short Indic stems survive
# Whisper's spelling drift.

# PHARMACY — ONLY supply / logistics phrasing. Anything about taking a drug,
# timing, dosing or side-effects must stay with the nurse, so those words are
# deliberately ABSENT here (they live in the Medication→nurse path).
_PHARMACY_SUPPLY = [
    # English — supply chain, not administration
    "prescription hasnt", "prescription has not", "prescription not arrived",
    "prescription didnt come", "prescription did not come", "no prescription",
    "refill", "re fill", "out of stock", "ran out of my", "ran out of medicine",
    "ran out of medication", "pharmacy didnt send", "pharmacy did not send",
    "pharmacy hasnt sent", "from the pharmacy", "collect my medicine",
    "pick up my medicine", "medicine not delivered", "medication not delivered",
    "resupply", "new strip", "new pack of",
    # Hindi (Devanagari, normalised) — "pharmacy se nahi aaya", "dawai khatam"
    "farmes se", "farmesi se", "dawai khatam", "dva khatam", "dawai nahi aayi",
    "dva nhi aayi", "parchi nahi aayi",
    # Hindi romanized
    "pharmacy se nahi", "dawai khatam", "refill chahi", "dva khatam ho",
    # Kannada (native + romanized) — "medicine barilla / khali aagide"
    "audhada angadi", "medicine barilla", "aushadha khali", "refill beku",
    # German
    "rezept nicht", "kein rezept", "apotheke hat nicht", "nachschub",
    "medikament nicht geliefert", "neue packung",
]

# HOUSEKEEPING — environmental only. NOT personal care (that is the nurse's).
_HOUSEKEEPING_ENV = [
    # English — the room / environment / facilities
    "dirty bathroom", "clean the bathroom", "clean the toilet", "toilet is dirty",
    "bathroom is dirty", "change the bedsheet", "change the bed sheet",
    "change the sheets", "change bedsheets", "fresh sheets", "clean sheets",
    "mop the floor", "floor is dirty", "clean the floor", "spill on the floor",
    "bin is full", "dustbin", "trash", "garbage", "empty the bin", "rubbish",
    "room is dirty", "clean the room", "clean my room", "tidy the room",
    "window is dirty", "clean the window",
    # Facility / maintenance — these classify as Other/Information today and get
    # suppressed; routing them here is the whole reason facility requests work.
    "ac is not working", "ac not working", "air conditioner", "air conditioning",
    "ac is broken", "ac isnt working", "fan not working", "fan is not working",
    "fan isnt working", "fan broken", "fan is broken", "ac is not cooling",
    "tv not working", "tv is not working", "tv isnt working", "tv is broken",
    "television not working", "remote not working", "remote isnt working",
    "light is out", "light not working", "lights not working", "light isnt working",
    "light is broken", "bulb is gone",
    "bulb fused", "tube light", "no light", "too hot in the room", "too cold in the room",
    "room is too hot", "room is too cold", "temperature in the room",
    "water leak", "leaking", "tap is broken", "tap not working", "flush not working",
    "toilet not flushing", "door wont close", "door is broken", "window wont close",
    "socket not working", "plug not working", "charging point", "wifi not working",
    # Hindi (Devanagari, normalised)
    "kamra ganda", "safai", "safaai", "bistar badlo", "chadar badlo",
    "ac kharab", "pankha kharab", "tv kharab", "batti kharab", "latt kharab",
    "nal kharab", "bathroom ganda", "kachra", "kuda",
    # Hindi romanized
    "kamra ganda", "chadar badlo", "ac kharab", "pankha nahi chal", "light kharab",
    "safai karo", "bathroom ganda", "kachra",
    # Kannada (romanized + native fragments)
    "room gandhu", "swachcha", "ac kelsa", "fan kelsa", "light illa",
    # German
    "zimmer ist schmutzig", "bettwasche wechseln", "boden wischen", "mulleimer",
    "klimaanlage", "ventilator kaputt", "fernseher geht nicht", "licht geht nicht",
    "lampe kaputt", "wasser lauft aus", "toilette ist schmutzig",
]

# HYGIENE — personal care that must stay with the NURSE even though the intent
# model labels it Hygiene. If any of these match, we do NOT send to housekeeping.
_HYGIENE_PERSONAL = [
    "soiled", "soil the bed", "wet the bed", "wet myself", "help me bathe",
    "help me shower", "help me wash", "give me a bath", "change my diaper",
    "change my pad", "change my nappy", "my diaper", "my pad", "bedpan",
    "clean me", "i am dirty", "incontinent", "accident in bed", "messed myself",
    # Hindi
    "nahla do", "saaf karo mujhe", "bistar gila", "diaper badlo",
    # German
    "mich waschen", "mich baden", "windel wechseln", "bettpfanne",
]

# CANTEEN — food/water requests (used to confirm a Food/Water intent is really a
# catering request; the diet gate in route_departments can still force nurse).
_CANTEEN = [
    "water", "hungry", "thirsty", "food", "meal", "eat", "drink", "juice",
    "tea", "coffee", "breakfast", "lunch", "dinner", "snack", "biscuit",
    # Hindi
    "pani", "paani", "bhookh", "khana", "chai", "pyaas",
    # Kannada
    "neeru", "oota", "hasivu",
    # German
    "wasser", "hunger", "durst", "essen", "trinken", "mahlzeit",
]


def _normset(frags: list[str]) -> list[str]:
    out = []
    for f in frags:
        nf = _normalize(f)
        if nf:
            out.append(nf)
    return out


_PHARMACY_NORM     = _normset(_PHARMACY_SUPPLY)
_HOUSEKEEPING_NORM = _normset(_HOUSEKEEPING_ENV)
_HYGIENE_PERS_NORM = _normset(_HYGIENE_PERSONAL)
_CANTEEN_NORM      = _normset(_CANTEEN)


def _first_hit(norm_text: str, fragments: list[str]) -> str | None:
    for frag in fragments:
        if frag and frag in norm_text:
            return frag
    return None


# Diet restriction values that mean "do not send food straight to the canteen —
# a nurse must check first". Empty / "none" means the canteen path is allowed.
_DIET_GATED = {"npo", "dysphagia", "diabetic", "low_sodium", "fluid_restricted", "renal"}


def _nurse(reason: str, *, is_primary: bool = True, confidence: float = 1.0) -> DepartmentDecision:
    return DepartmentDecision(
        department=Department.NURSE,
        confidence=confidence,
        routing_reason="intent_map",
        reason=reason,
        is_primary=is_primary,
    )


def route_departments(
    transcript: str,
    intent: Intent,
    *,
    diet_restriction: str | None = None,
    stt_avg_logprob: float | None = None,
    stt_corrected: bool = False,
    min_confidence: float = 0.85,
    min_stt_avg_logprob: float = -0.7,
) -> list[DepartmentDecision]:
    """Decide where a request goes. Returns a non-empty list of decisions.

    The list ALWAYS contains exactly one `is_primary=True` decision — the party
    that receives the FULL request. For a clean single-department non-nurse
    request the primary IS that department. When a request mixes clinical and
    non-clinical content, the primary is forced to the NURSE and the other
    departments appear as secondary (is_primary=False) notifications.

    Parameters mirror what the pipeline can supply; all the ML-facing knobs
    (confidence, stt quality) are plumbed so Phase 2 can pass a model score
    without changing callers.
    """
    text = transcript or ""
    norm = _normalize(text)
    diet = (diet_restriction or "").strip().lower()

    # Detect a pharmacy SUPPLY phrase up front. The priority engine's urgent
    # markers include generic medication words ("medicine"/"medication"), so a
    # supply request like "refill of my medicine" would otherwise be swallowed by
    # the urgent-marker early-return below and never reach pharmacy. We let a
    # pure supply request (no CRITICAL marker) fall through to normal assembly,
    # where the nurse stays primary (medication is clinical) AND pharmacy gets a
    # secondary notification — the safe, informative outcome.
    pharmacy_hit = _first_hit(norm, _PHARMACY_NORM)

    # ── Hard safety overrides: the nurse takes the whole thing ────────────────
    # 1. Critical/urgent markers in the transcript (life-threatening cues, "call
    #    the doctor", etc.) — reuse the exact same detectors the priority engine
    #    uses so the two can never disagree.
    if priority_engine.has_critical_marker(text):
        return [_nurse("critical marker present — nurse handles the full request")]
    if priority_engine.has_urgent_marker(text) and not pharmacy_hit:
        return [_nurse("urgent marker present — nurse handles the full request")]

    # 2. Clinical intents always go to the nurse, full stop.
    clinical = {
        Intent.EMERGENCY, Intent.PAIN, Intent.MEDICATION,
        Intent.MOBILITY, Intent.EMOTIONAL_SUPPORT,
    }
    # (Medication is intentionally clinical: administration/timing/dosing/side
    #  effects are the nurse's. A supply-only phrasing is handled below, where
    #  the nurse still receives it AND pharmacy gets a secondary notification.)

    # 3. STT is unreliable → veto all non-nurse routing.
    stt_ok = True
    if stt_avg_logprob is not None and stt_avg_logprob < min_stt_avg_logprob:
        stt_ok = False
    if stt_corrected:
        stt_ok = False

    # ── Detect candidate non-nurse slices from the transcript ─────────────────
    # (pharmacy_hit already computed above, before the urgent-marker gate)
    housekeeping_hit = _first_hit(norm, _HOUSEKEEPING_NORM)
    personal_hygiene = _first_hit(norm, _HYGIENE_PERS_NORM) is not None
    canteen_hit      = _first_hit(norm, _CANTEEN_NORM)

    secondary: list[DepartmentDecision] = []
    nurse_needed = False
    nurse_reasons: list[str] = []

    # If STT is unreliable, collapse straight to the nurse regardless of hits.
    if not stt_ok:
        why = "low STT confidence" if (stt_avg_logprob is not None and
              stt_avg_logprob < min_stt_avg_logprob) else "transcript auto-corrected (script ambiguity)"
        return [_nurse(f"{why} — nurse decides")]

    # Clinical intent → nurse is primary; still scan for tack-on facility/supply
    # requests so "give my medicine and the AC is broken" also pings housekeeping.
    if intent in clinical:
        nurse_needed = True
        nurse_reasons.append(f"clinical intent {intent.value}")

    # ── PHARMACY (supply only) ────────────────────────────────────────────────
    if pharmacy_hit:
        secondary.append(DepartmentDecision(
            department=Department.PHARMACY, confidence=0.9, routing_reason="keyword",
            reason="medication supply/logistics request", matched=pharmacy_hit,
        ))
        # Pharmacy supply requests also inform the nurse (they own the patient's
        # medication chart) — so the nurse gets a notice too.
        nurse_needed = True
        nurse_reasons.append("pharmacy supply request (nurse informed)")

    # ── HOUSEKEEPING (environmental) ──────────────────────────────────────────
    # Only if there is NOT a personal-care hygiene phrase (that is the nurse's).
    if housekeeping_hit and not personal_hygiene:
        secondary.append(DepartmentDecision(
            department=Department.HOUSEKEEPING, confidence=0.9, routing_reason="keyword",
            reason="environmental / facility request", matched=housekeeping_hit,
        ))

    # Personal-care hygiene always means the nurse.
    if personal_hygiene:
        nurse_needed = True
        nurse_reasons.append("personal-care hygiene (nurse)")

    # Hygiene intent with no explicit environmental phrase → nurse (ambiguous
    # between personal care and environment; safety-first sends it to the nurse).
    if intent == Intent.HYGIENE and not housekeeping_hit:
        nurse_needed = True
        nurse_reasons.append("hygiene intent, ambiguous — nurse")

    # ── CANTEEN (food/water) with the diet gate ───────────────────────────────
    is_food = intent == Intent.FOOD_WATER or canteen_hit is not None
    if is_food:
        if diet in _DIET_GATED:
            # Diet restriction: a nurse must verify before any food is sent.
            nurse_needed = True
            nurse_reasons.append(f"diet restriction '{diet}' — nurse must check before catering")
        else:
            secondary.append(DepartmentDecision(
                department=Department.CANTEEN, confidence=0.88, routing_reason="keyword",
                reason="food/water request (no diet restriction)", matched=canteen_hit or "food/water intent",
            ))
            # Canteen request still gets an actionable notice to the nurse.
            nurse_needed = True
            nurse_reasons.append("catering request (nurse informed)")

    # ── Assemble the decision list ────────────────────────────────────────────
    # Apply the asymmetric confidence gate to every non-nurse decision: anything
    # below the floor is dropped and (if nothing else routes) the nurse gets it.
    gated_secondary = [d for d in secondary if d.confidence >= min_confidence]
    dropped = [d for d in secondary if d.confidence < min_confidence]
    if dropped:
        nurse_needed = True
        nurse_reasons.append("low-confidence slice folded back to nurse")

    if not gated_secondary:
        # Nothing routed anywhere else → the nurse owns the whole request
        # (covers Information/Other that isn't a facility request, and the
        # general / undecipherable case the spec calls out).
        reason = "; ".join(nurse_reasons) or "general or undecipherable request — default to nurse"
        return [_nurse(reason)]

    if nurse_needed:
        # Mixed request: nurse is primary (full request) + department notices.
        primary = _nurse("; ".join(nurse_reasons))
        return [primary, *gated_secondary]

    # Pure single/multi non-nurse request: the first (highest-priority) slice is
    # the primary carrier; others ride along. No nurse involvement.
    gated_secondary[0].is_primary = True
    return gated_secondary
