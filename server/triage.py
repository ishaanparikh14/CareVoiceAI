# -*- coding: utf-8 -*-
"""
triage.py — decide the request class BEFORE an alert is created.

Routing model (from the "How the router decides the route" slide):

  * Nurse-bound intents (emergency, pain, medicine, emotional support, mobility,
    personal care, general/unclear) go DIRECTLY to the nurse pipeline and never
    enter the router, so urgent calls get no extra hop.
  * Only food and housekeeping / ground-support text enters the router:
        d* = argmax p(d | x) over {Canteen, Floor manager}
        route = d*      if p(d*) >= tau_r
        route = Nurse   otherwise

Triage runs before priority/alert creation so a DistilBERT mislabel can no longer
turn "I have spilled water on the floor" (model said Medication) into a nurse
CRITICAL alert: a facility request is stored as Routine / Facility.

Food is special: patients cannot order food themselves. A patient's spoken food
request becomes a nurse-visible request flagged as food; the attender (from the
patient dashboard) or the nurse places the actual canteen order with the diet
applied. So request_class 'food' stays visible to the nurse but at Routine.

classify_text() is pure (no I/O) and unit-tested. triage() adds the optional LLM.
"""

from __future__ import annotations

from dataclasses import dataclass

import priority_engine
from models import Intent

# Intents that always belong to the nurse.
NURSE_INTENTS = {
    Intent.EMERGENCY, Intent.PAIN, Intent.MEDICATION,
    Intent.EMOTIONAL_SUPPORT, Intent.MOBILITY,
}

# Clinical words that keep a request with the nurse even when a facility
# keyword is also present ("my chest hurts and the AC is broken").
_CLINICAL_WORDS = [
    "pain", "hurt", "ache", "chest", "breath", "bleed", "dizzy", "vomit",
    "fever", "faint", "medicine", "medication", "tablet", "injection", "doctor",
    "nurse", "fell", "fall down", "scared",
    "dard", "dawai", "dawa", "bukhar", "chakkar", "ulti",
    "दरद", "दवई", "दव", "बखर", "चककर", "उलट", "डकटर",
    "schmerz", "arzt", "medikament",
]


def _clinical_norm() -> list[str]:
    from department_router import _normalize
    return [n for n in (_normalize(w) for w in _CLINICAL_WORDS) if n]


_CLINICAL_NORM = _clinical_norm()

# Feelings: never treat these as small talk (emotional support is real work).
_FEELING_WORDS = [
    "lonely", "alone", "sad", "afraid", "fear", "worried", "anxious", "depressed",
    "cry", "crying", "upset", "nervous", "panic", "help", "please",
    "akela", "udaas", "ghabra", "pareshan", "rona",
    "अकेल", "उदास", "घबर", "परेशान", "डर",
]
_FEELING_NORM = [n for n in (__import__("department_router")._normalize(w) for w in _FEELING_WORDS) if n]


@dataclass
class Triage:
    request_class: str          # "nurse" | "facility" | "food"
    intent: Intent              # intent to store on the alert
    confidence: float           # p(d*) for routed classes; 1.0 for nurse
    reason: str
    source: str                 # "nurse_direct" | "keyword" | "llm" | "fallback"
    model_version: str = "kw-router-v2"


def _nurse(intent: Intent, reason: str) -> Triage:
    return Triage("nurse", intent, 1.0, reason, "nurse_direct")


def classify_text(
    transcript: str,
    model_intent: Intent,
    *,
    tau_r: float = 0.6,
    stt_avg_logprob: float | None = None,
    stt_corrected: bool = False,
    min_stt_avg_logprob: float = -0.7,
) -> Triage:
    """Keyword triage. Pure function; never raises for normal input."""
    from department_router import (
        _normalize, _first_hit, _HOUSEKEEPING_NORM, _HYGIENE_PERS_NORM, _CANTEEN_NORM,
    )
    text = transcript or ""

    # 1. Safety first: life-threatening or urgent cues are always the nurse's.
    if priority_engine.has_critical_marker(text):
        return _nurse(model_intent if model_intent in NURSE_INTENTS else Intent.EMERGENCY,
                      "critical marker — nurse direct")

    norm = _normalize(text)
    facility_hit = _first_hit(norm, _HOUSEKEEPING_NORM)
    personal_care = _first_hit(norm, _HYGIENE_PERS_NORM) is not None
    food_hit = _first_hit(norm, _CANTEEN_NORM)

    # Personal care (bathe me, soiled bed) is nursing work, never housekeeping.
    if personal_care:
        return _nurse(Intent.HYGIENE, "personal care — nurse direct")

    # 2. Unreliable speech-to-text: let a human decide.
    if (stt_avg_logprob is not None and stt_avg_logprob < min_stt_avg_logprob) or stt_corrected:
        return _nurse(model_intent, "low STT confidence — nurse decides")

    # 3. Ground-support / facility text. A facility keyword overrides a weak
    #    clinical LABEL (the DistilBERT mislabels: "spilled water" -> Medication),
    #    but not real clinical content: pain/emergency labels, urgent cues, or a
    #    clinical word in the text ("my chest hurts and the AC is broken") keep
    #    the request with the nurse.
    clinical_text = _first_hit(norm, _CLINICAL_NORM) is not None
    strong_clinical = model_intent in (Intent.PAIN, Intent.EMERGENCY, Intent.EMOTIONAL_SUPPORT)
    if (facility_hit and not priority_engine.has_urgent_marker(text)
            and not clinical_text and not strong_clinical):
        conf = 0.9
        if conf >= tau_r:
            return Triage("facility", Intent.FACILITY, conf,
                          f"facility keyword '{facility_hit}'", "keyword")
        return _nurse(model_intent, "facility below tau_r — nurse fallback")

    # 4. Clinical label with no facility cue → nurse direct (skips the router).
    if model_intent in NURSE_INTENTS:
        return _nurse(model_intent, f"{model_intent.value} — nurse direct")

    # 5. Food / water.
    if (model_intent == Intent.FOOD_WATER or food_hit) and not priority_engine.has_urgent_marker(text):
        conf = 0.88 if food_hit else 0.7
        if conf >= tau_r:
            return Triage("food", Intent.FOOD_WATER, conf,
                          f"food request ({food_hit or 'food intent'})", "keyword")

    # 6. Everything else (general, unclear, hygiene without an env. cue) → nurse.
    return _nurse(model_intent, "general / unclear — nurse")


NO_REQUEST_MIN_CONF = 0.8
# Looser than DEPT_MIN_STT_AVG_LOGPROB (-0.7): only used for the small-talk check.
SMALL_TALK_MIN_STT_LOGPROB = -1.0


def is_safe_to_drop(text: str, model_intent: Intent, llm_conf: float) -> bool:
    """A 'not a request' verdict may only suppress the nurse page when nothing
    clinical is present: no pain/emergency/medicine label, no urgent or critical
    cue, no clinical word, and a confident LLM."""
    from department_router import _normalize, _first_hit
    if llm_conf < NO_REQUEST_MIN_CONF:
        return False
    if model_intent in (Intent.EMERGENCY, Intent.PAIN, Intent.MEDICATION):
        return False
    if priority_engine.has_critical_marker(text) or priority_engine.has_urgent_marker(text):
        return False
    norm = _normalize(text)
    if _first_hit(norm, _CLINICAL_NORM) is not None or _first_hit(norm, _FEELING_NORM) is not None:
        return False
    return True


def from_llm(
    transcript: str,
    model_intent: Intent,
    llm: dict,
    *,
    tau_r: float = 0.6,
    stt_avg_logprob: float | None = None,
    stt_corrected: bool = False,
    min_stt_avg_logprob: float = -0.7,
) -> Triage:
    """Turn an LLM classification into a Triage, keeping the hard safety gates."""
    text = transcript or ""
    if priority_engine.has_critical_marker(text):
        return _nurse(model_intent if model_intent in NURSE_INTENTS else Intent.EMERGENCY,
                      "critical marker — nurse direct")

    dep = str(llm.get("department", "nurse")).lower()
    conf = float(llm.get("confidence", 0.0) or 0.0)
    needs_nurse = bool(llm.get("needs_nurse", False))
    why = (llm.get("reason") or dep)[:120]

    # Small talk ("nice weather!"): not a request. Dropped only when every
    # safety signal agrees; otherwise the nurse still gets it. Whisper scores
    # correctly-heard 2-3 word clips around -0.7..-0.9, so this check uses a
    # looser STT floor than department routing — the patient sees the
    # transcript and a Call Nurse button, so a mishearing is visible to them.
    stt_ok_for_drop = (not stt_corrected and
                       (stt_avg_logprob is None or stt_avg_logprob >= SMALL_TALK_MIN_STT_LOGPROB))
    if llm.get("is_request") is False and stt_ok_for_drop and is_safe_to_drop(text, model_intent, conf):
        return Triage("none", model_intent, conf, f"llm: not a request ({why})", "llm", "llm")

    if (stt_avg_logprob is not None and stt_avg_logprob < min_stt_avg_logprob) or stt_corrected:
        return _nurse(model_intent, "low STT confidence — nurse decides")

    # Pharmacy is no longer a patient-facing destination: medicine → nurse.
    if dep in ("nurse", "pharmacy") or needs_nurse:
        intent = Intent.MEDICATION if dep == "pharmacy" else model_intent
        return Triage("nurse", intent, 1.0, f"llm: {why}", "llm", "llm")
    if conf < tau_r:
        return Triage("nurse", model_intent, conf, f"llm below tau_r ({conf:.2f})", "llm", "llm")
    if dep == "housekeeping":
        return Triage("facility", Intent.FACILITY, conf, f"llm: {why}", "llm", "llm")
    if dep == "canteen":
        return Triage("food", Intent.FOOD_WATER, conf, f"llm: {why}", "llm", "llm")
    return _nurse(model_intent, "llm returned unknown department — nurse")


async def triage(
    transcript: str,
    model_intent: Intent,
    *,
    cfg: dict,
    stt_avg_logprob: float | None = None,
    stt_corrected: bool = False,
) -> Triage:
    """Full triage: keyword first; if the text could be food/facility (or the
    model label is ambiguous) and the LLM is enabled, let the LLM decide.

    Nurse-bound labels with no food/facility cue never call the LLM, so urgent
    calls are not delayed by an extra network hop."""
    from config import settings
    tau_r = float(cfg.get("tau_r", 0.6))
    if (transcript or "").strip() in ("", "[silence]"):
        return Triage("none", model_intent, 1.0, "no speech heard", "keyword")
    kw = classify_text(
        transcript, model_intent, tau_r=tau_r,
        stt_avg_logprob=stt_avg_logprob, stt_corrected=stt_corrected,
        min_stt_avg_logprob=settings.DEPT_MIN_STT_AVG_LOGPROB,
    )
    # Critical / personal-care are final.
    if kw.reason.startswith(("critical marker", "personal care")):
        return kw
    # Unreliable STT: final for routing. The LLM is consulted only to spot
    # small talk (from_llm drops it only if the looser STT floor and every
    # safety gate pass; anything else returns the nurse).
    if kw.reason.startswith("low STT"):
        could_be_small_talk = (
            not stt_corrected
            and (stt_avg_logprob is None or stt_avg_logprob >= SMALL_TALK_MIN_STT_LOGPROB)
            and model_intent not in (Intent.EMERGENCY, Intent.PAIN, Intent.MEDICATION)
        )
        if not (cfg.get("llm_classifier", True) and could_be_small_talk):
            return kw
        try:
            from llm_classifier import classify as llm_classify
            llm = await llm_classify(transcript)
        except Exception:
            llm = None
        if llm is None:
            return kw
        t = from_llm(transcript, model_intent, llm, tau_r=tau_r,
                     stt_avg_logprob=stt_avg_logprob, stt_corrected=stt_corrected,
                     min_stt_avg_logprob=settings.DEPT_MIN_STT_AVG_LOGPROB)
        return t if t.request_class == "none" else kw

    # Only spend an LLM call when the request might be routable or the model
    # label is a known weak spot (Other/Information/Hygiene/Mobility/Medication
    # are where the screenshot mislabels came from).
    # Emotional Support is included because the intent model also puts small
    # talk there ("Nice weather!" -> Emotional Support); real distress is kept
    # with the nurse by the LLM prompt and is_safe_to_drop().
    ambiguous = model_intent in {Intent.OTHER, Intent.INFORMATION, Intent.HYGIENE,
                                 Intent.MOBILITY, Intent.MEDICATION, Intent.FOOD_WATER,
                                 Intent.EMOTIONAL_SUPPORT}
    worth_llm = kw.request_class != "nurse" or ambiguous
    if cfg.get("llm_classifier", True) and worth_llm:
        try:
            from llm_classifier import classify as llm_classify
            llm = await llm_classify(transcript)
        except Exception:
            llm = None
        if llm is not None:
            return from_llm(
                transcript, model_intent, llm, tau_r=tau_r,
                stt_avg_logprob=stt_avg_logprob, stt_corrected=stt_corrected,
                min_stt_avg_logprob=settings.DEPT_MIN_STT_AVG_LOGPROB,
            )
    return kw
