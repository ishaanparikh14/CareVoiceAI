# -*- coding: utf-8 -*-
"""
nlp_summary.py — CareVoice AI Layer 3 NLP summary + emotional-intelligence module.

This is the "NLP keyword extract" component from the architecture (Layer 3):
given the raw STT transcript and the classified intent, it produces

  1. a short, human-readable REQUEST SUMMARY (what the patient wants),
  2. an EMOTION / distress signal (calm | anxious | distressed | panicked),
  3. the standardised ALERT MESSAGE delivered to the nurse station:

        "Patient <name> in Room <room> is calling — <emotion clause> — <summary>"

Design notes
------------
• Deterministic + on-premises (no cloud calls, matches the LAN-only constraint).
• Multilingual: reuses the same Unicode-normalisation trick as intent_keywords.py
  (lowercase + strip Indic combining marks) so Hindi/Kannada spelling drift from
  Whisper still matches emotion cues.
• Intent-driven summary with a keyword-refinement pass: the intent gives the base
  request ("needs medication"), and specific keywords refine it ("needs a pain
  killer", "wants water"). This keeps summaries reliable without a heavyweight
  summarisation model, while remaining easy to extend.
• The emotion signal fuses (a) explicit emotion words in the transcript and
  (b) the intent itself (Emergency ⇒ at least distressed). It maps to a numeric
  distress magnitude in [0,1] so the existing distress_score column can carry it.
"""

import unicodedata

from models import Intent


# ── Normalisation (shared approach with intent_keywords / priority_engine) ────

def _normalize(text: str) -> str:
    """Lowercase + strip Unicode combining marks so diacritic variants collapse."""
    t = (text or "").lower().strip()
    decomposed = unicodedata.normalize("NFD", t)
    stripped = "".join(
        ch for ch in decomposed if unicodedata.category(ch) not in ("Mn", "Mc")
    )
    return unicodedata.normalize("NFC", stripped)


def _contains(norm_text: str, fragments: list[str]) -> bool:
    for frag in fragments:
        nf = _normalize(frag).replace(" ", " ").strip()
        core = nf.replace(" ", "")
        if not core:
            continue
        is_indic = any(0x0900 <= ord(c) <= 0x0DFF for c in core)
        min_len = 3 if is_indic else 4
        if len(core) < min_len:
            continue
        if nf in norm_text:
            return True
    return False


# ── Emotion / distress lexicon (multilingual) ─────────────────────────────────
# Ordered strongest-first. Each level carries a distress magnitude.

# Panic / acute distress — screaming for help, can't cope.
_PANIC_WORDS = [
    "help me", "please help", "somebody help", "save me", "cant take", "can not take",
    "dying", "i cant", "please please",
    "बचव", "बचओ", "मर रह", "बहत तज", "सहन नह",           # Hindi
    "bachao", "mar raha", "sahan nahi",
    "ಸಹಾಯ ಮಾಡಿ", "ಬದುಕಿಸಿ", "ತಡೆಯಲಾಗ",                        # Kannada
    "sahaya madi", "kapadi",
]

# Distressed — pain/fear expressed strongly.
_DISTRESS_WORDS = [
    "scared", "terrified", "frightened", "panic", "cant bear", "unbearable",
    "very bad", "so much pain", "terrible", "worst",
    "डर लग", "बहत दरद", "घबर", "बरदशत नह",                  # Hindi
    "dar lag", "bahut dard", "ghabra",
    "ಭಯ", "ತುಂಬಾ ನೋವು", "ಆತಂಕ",                              # Kannada
    "bhaya", "tumba novu",
]

# Anxious — worried, uneasy, uncomfortable.
_ANXIOUS_WORDS = [
    "worried", "anxious", "uneasy", "uncomfortable", "nervous", "restless",
    "please come", "please hurry", "quickly",
    "चत", "बचन", "जलद आओ", "परशन",                          # Hindi
    "chinta", "jaldi aao", "pareshan",
    "ಚಿಂತೆ", "ಬೇಗ ಬನ್ನಿ", "ಆತಂಕ",                             # Kannada
    "chinte", "bega banni",
]


# Emotion → (label, distress magnitude 0..1)
_EMOTION_LEVELS = [
    ("panicked",   0.95, _PANIC_WORDS),
    ("distressed", 0.75, _DISTRESS_WORDS),
    ("anxious",    0.50, _ANXIOUS_WORDS),
]


def detect_emotion(transcript: str, intent: Intent) -> tuple[str, float]:
    """
    Return (emotion_label, distress_magnitude 0..1).

    Fuses explicit emotion words with the intent: an Emergency intent is at
    least 'distressed' even if the transcript has no emotion words (the patient
    may just say "I can't breathe" flatly). Emotional-Support intent implies at
    least 'anxious'.
    """
    t = _normalize(transcript)

    detected = "calm"
    magnitude = 0.1
    for label, mag, words in _EMOTION_LEVELS:
        if _contains(t, words):
            detected, magnitude = label, mag
            break

    # Intent-based floor.
    if intent == Intent.EMERGENCY:
        if magnitude < 0.75:
            detected, magnitude = "distressed", 0.8
    elif intent == Intent.EMOTIONAL_SUPPORT:
        if magnitude < 0.5:
            detected, magnitude = "anxious", 0.5
    elif intent == Intent.PAIN:
        if magnitude < 0.5:
            detected, magnitude = "anxious", 0.5

    return detected, round(magnitude, 2)


# ── Request summary ────────────────────────────────────────────────────────────
# Base phrasing per intent, plus keyword refinements that make the summary
# specific (e.g. "needs a painkiller" instead of just "needs medication").

_INTENT_SUMMARY = {
    Intent.EMERGENCY:          "medical emergency — needs immediate help",
    Intent.PAIN:               "is in pain and needs relief",
    Intent.MEDICATION:         "is asking for medication",
    Intent.FOOD_WATER:         "needs food or water",
    Intent.MOBILITY:           "needs mobility assistance",
    Intent.HYGIENE:            "needs hygiene assistance",
    Intent.EMOTIONAL_SUPPORT:  "would like emotional support",
    Intent.INFORMATION:        "has a question",
    Intent.OTHER:              "needs assistance",
}

# Keyword → refined summary phrase. Checked within the matching intent context
# but also globally as a fallback so specific requests surface even if the
# intent label is generic.
_REFINEMENTS: list[tuple[list[str], str]] = [
    # Emergency specifics
    (["cant breathe", "cannot breathe", "breathing", "सस नह", "usiradalu", "ಉಸಿರಾಡ"],
     "cannot breathe — respiratory emergency"),
    (["chest pain", "heart", "सन म दरद", "ede novu", "ಎದೆ ನೋವು"],
     "chest pain — possible cardiac emergency"),
    (["bleeding", "खन", "ರಕ್ತ"], "is bleeding — needs urgent attention"),
    (["fall", "fell", "gir gaya", "गर गय"], "has fallen and needs help"),
    # Medication specifics
    (["painkiller", "pain killer", "dard ki dawa", "दरद क दव"], "needs a painkiller"),
    (["insulin", "इनसलन", "ಇನ್ಸುಲಿನ್"], "needs insulin"),
    (["injection", "इनजकशन", "ಚುಚ್ಚುಮದ್ದು"], "is asking for an injection"),
    # Food/Water specifics
    (["water", "पन", "पानी", "neeru", "ನೀರು"], "wants water"),
    (["hungry", "food", "भख", "खन", "hasivu", "ಹಸಿವು", "ಆಹಾರ"], "is hungry and wants food"),
    # Mobility specifics
    (["bathroom", "toilet", "washroom", "बथरम", "शौचालय", "ಶೌಚಾಲ"], "needs to use the bathroom"),
    (["wheelchair", "वहलचयर", "ಗಾಲಿಕುರ್"], "needs a wheelchair"),
    (["get up", "stand", "उठन", "eddel", "ಎದ್ದೇಳ"], "wants help getting up"),
    # Hygiene specifics
    (["bath", "shower", "नहन", "snaana", "ಸ್ನಾನ"], "would like a bath"),
    (["change sheets", "चदर बदल", "ಹಾಸಿಗೆ"], "needs bed sheets changed"),
    (["diaper", "डयपर"], "needs a diaper change"),
    # Emotional
    (["someone to talk", "stay with me", "lonely", "अकल", "onti", "ಒಂಟಿ"],
     "feels lonely and wants someone to talk to"),
]


def summarize_request(transcript: str, intent: Intent) -> str:
    """
    Produce a short, nurse-readable summary of what the patient wants.
    Starts from the intent's base phrasing, then applies the most specific
    keyword refinement found in the transcript.
    """
    t = _normalize(transcript)
    for fragments, phrase in _REFINEMENTS:
        if _contains(t, fragments):
            return phrase
    return _INTENT_SUMMARY.get(intent, "needs assistance")


# ── Emotion clause for the spoken/standardised message ─────────────────────────

_EMOTION_CLAUSE = {
    "panicked":   "the patient sounds panicked",
    "distressed": "the patient sounds distressed",
    "anxious":    "the patient sounds anxious",
    "calm":       "the patient sounds calm",
}


def build_alert_message(
    *,
    patient_name: str | None,
    room_id: str,
    intent: Intent,
    transcript: str,
    emotion: str | None = None,
) -> str:
    """
    Compose the standardised nurse-station message:

        "Patient <name> in Room <room> is calling — <emotion clause> — <summary>."

    The fixed first part is identical for every request (per the architecture's
    Auto-Message Generator); the NLP summary + emotion are appended.
    """
    name = (patient_name or "").strip() or "the patient"
    summary = summarize_request(transcript, intent)
    if emotion is None:
        emotion, _ = detect_emotion(transcript, intent)
    clause = _EMOTION_CLAUSE.get(emotion, "the patient sounds calm")

    prefix = f"Patient {name} in Room {room_id} is calling"
    return f"{prefix} — {clause} — {summary}."


def analyze(
    *,
    transcript: str,
    intent: Intent,
    room_id: str,
    patient_name: str | None = None,
) -> dict:
    """
    One-shot convenience: returns everything the pipeline needs.

    {
      "summary":      "wants water",
      "emotion":      "anxious",
      "distress":     0.5,
      "alert_message":"Patient Rajesh in Room 4B is calling — the patient sounds anxious — wants water."
    }
    """
    emotion, distress = detect_emotion(transcript, intent)
    summary = summarize_request(transcript, intent)
    message = build_alert_message(
        patient_name=patient_name,
        room_id=room_id,
        intent=intent,
        transcript=transcript,
        emotion=emotion,
    )
    return {
        "summary": summary,
        "emotion": emotion,
        "distress": distress,
        "alert_message": message,
    }
