# -*- coding: utf-8 -*-
"""
priority_engine.py — CareVoice AI Layer 4 Priority Engine.

Priority is driven purely by the classified INTENT, with one safety override:
a life-threatening phrase in the transcript ("can't breathe", "chest pain", …)
forces Critical even if the intent model guessed something milder. There is no emotion/distress estimator — the intent taxonomy encodes urgency and is
language-independent (the multilingual classifier outputs one of 9 fixed labels
regardless of input language).

Intent → Priority mapping:

    Critical : Emergency or explicit life-threatening markers
    Urgent   : Pain, Medication, or explicit urgent markers
    Routine  : Food/Water, Mobility, Hygiene, Emotional Support, Information, Other

Nurse notification:
    Any genuine care intent → notify.
    Information / Other      → suppress, UNLESS a critical marker is present.
"""

from models import Intent, Priority

# Intents mapped to each priority level.
CRITICAL_INTENTS = {Intent.EMERGENCY}
URGENT_INTENTS   = {Intent.PAIN, Intent.MEDICATION}

# Genuine care requests — nurse is always notified for these.
CARE_INTENTS = {
    Intent.EMERGENCY, Intent.PAIN, Intent.MEDICATION, Intent.FOOD_WATER,
    Intent.MOBILITY, Intent.HYGIENE, Intent.EMOTIONAL_SUPPORT,
}

# Ambient / informational — suppressed unless a critical marker is present.
NON_ALERT_INTENTS = {Intent.INFORMATION, Intent.OTHER}


# ── Critical-marker safety net ────────────────────────────────────────────────
# Unambiguous life-threatening cues that force Critical regardless of the intent
# model's guess. Because Whisper transcribes Indic speech inconsistently — it
# drops/adds vowel diacritics (सास vs साँस vs सांस), and sometimes renders Hindi
# in Urdu or Latin script — we (1) normalise the text (strip Indic combining
# marks, lowercase) and (2) match on short core fragments rather than exact
# phrases, across every script Whisper is likely to emit.
import unicodedata

CRITICAL_FRAGMENTS = [
    # ── "can't breathe" / breathlessness ──
    # English
    "cant breathe", "can not breathe", "cannot breathe", "not able to breathe",
    "cant breath", "trouble breathing", "hard to breathe", "short of breath",
    "gasping", "suffocat", "choking", "choke",
    # Hindi Devanagari (normalised — diacritics stripped, so सास/साँस/सांस all match "सस"/"सास")
    "सास नह", "सास नाह", "सस नह", "साँस नह", "सांस नह", "सन नह",
    "दम घुट", "दम घट", "घुट रह",
    # Hindi in Urdu script (Whisper sometimes emits this for Hindi speech)
    "اساس نہیں", "سانس نہیں", "سنس نہیں", "دم گھٹ",
    # Hindi romanized (Latin)
    "saans nah", "saas nah", "sans nah", "saans nah", "dam ghut",
    # Kannada
    "ಉಸಿರಾಡಲು", "ಉಸಿರು ಕಟ್ಟು", "ಉಸಿರಾಟ", "usiradalu", "usiru kattu",

    # ── chest pain / heart ──
    "chest pain", "heart attack", "सीने म दरद", "सन म दरद", "दल का दौर",
    "seene me dard", "chhati", "ಎದೆ ನೋವು", "ಹೃದಯಾಘಾತ", "ede novu", "hrudayaghata",

    # ── unconscious / collapse / bleeding ──
    "unconscious", "collaps", "faint", "बहश", "बेहश", "behosh", "behosh",
    "मरछ", "ಮೂರ್ಛೆ", "bleeding badly", "खन बह", "khoon", "ರಕ್ತ",
]


def _normalize(text: str) -> str:
    """
    Lowercase + strip Unicode combining marks (Indic matras/nukta/anusvara) so
    Whisper's inconsistent diacritics don't defeat substring matching.
    e.g. 'साँस' → 'सस', 'सांस' → 'सस', 'सास' → 'सस'.
    """
    t = text.lower().strip()
    # Decompose then drop combining marks (category Mn/Mc)
    decomposed = unicodedata.normalize("NFD", t)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) not in ("Mn", "Mc"))
    return unicodedata.normalize("NFC", stripped)


# ── Urgent-marker layer ───────────────────────────────────────────────────────
# Cues that should escalate a request to at least URGENT regardless of the intent
# model's label — most importantly, asking to see/call a doctor. Same normalise-
# and-fragment approach as the critical markers.
URGENT_FRAGMENTS = [
    # ── see / call the doctor ──
    "see the doctor", "see a doctor", "need the doctor", "need a doctor",
    "call the doctor", "call a doctor", "want the doctor", "want to see the doctor",
    "get the doctor", "doctor please", "need doctor",
    # Hindi (Devanagari, normalised)
    "डकटर क बल", "डकटर स मल", "डकटर च", "डकटर क दख", "डकटर बल",
    # Hindi romanized
    "doctor ko bula", "doctor chahi", "doctor se mil",
    # Kannada (native + romanized)
    "ವೈದ್ಯರನ್ನು", "ಡಾಕ್ಟರ್", "vaidyar", "doctor beku",
    # ── medication / pain adjacent phrasings that the model sometimes routes wrong ──
    "medicine", "medication", "tablet", "injection", "painkiller",
]


# Pre-normalise the fragment lists once at import.
_CRITICAL_NORM = [_normalize(f) for f in CRITICAL_FRAGMENTS]
_URGENT_NORM   = [_normalize(f) for f in URGENT_FRAGMENTS]


def has_critical_marker(transcript: str) -> bool:
    """True if the transcript contains an unambiguous life-threatening cue."""
    if not transcript:
        return False
    t = _normalize(transcript)
    return any(frag and frag in t for frag in _CRITICAL_NORM)


def has_urgent_marker(transcript: str) -> bool:
    """True if the transcript contains a cue that warrants at least Urgent."""
    if not transcript:
        return False
    t = _normalize(transcript)
    return any(frag and frag in t for frag in _URGENT_NORM)


def compute_priority(intent: Intent, transcript: str = "", distress_score: float = 0.0) -> Priority:
    """Map the classified intent and explicit emergency/urgent keywords to a tier.

    The distress/emotion score argument remains for backwards compatibility,
    but is intentionally ignored.
    """
    if intent in CRITICAL_INTENTS or has_critical_marker(transcript):
        return Priority.CRITICAL
    if intent in URGENT_INTENTS or has_urgent_marker(transcript):
        return Priority.URGENT
    return Priority.ROUTINE


def should_alert_nurse(intent: Intent, transcript: str = "", distress_score: float = 0.0) -> bool:
    """Every patient request is a nurse-call request in the current product."""
    return True
