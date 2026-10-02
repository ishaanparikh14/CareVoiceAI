# -*- coding: utf-8 -*-
"""
intent_keywords.py — high-precision keyword override for intent classification.

The multilingual DistilBERT model is good but not perfect, especially on the
spelling/script variants that Whisper produces for Hindi & Kannada speech
(e.g. "दवाईया" vs "दवाई" vs "दवा"). This module applies deterministic keyword
rules AFTER the model: if the transcript clearly contains words for a given
intent, we override the model's guess. This makes safety-relevant intents
(Emergency, Pain, Medication) reliable regardless of model noise.

Matching is done on NORMALISED text (lowercased, Indic combining marks stripped)
so diacritic variants collapse — see _normalize().  Order matters: the list is
checked most-urgent-first, so a phrase mentioning both pain and water resolves
to the more urgent intent.
"""

import unicodedata
from models import Intent


def _normalize(text: str) -> str:
    """Lowercase + strip Unicode combining marks so diacritic variants collapse."""
    t = (text or "").lower().strip()
    decomposed = unicodedata.normalize("NFD", t)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) not in ("Mn", "Mc"))
    return unicodedata.normalize("NFC", stripped)


# Intent → keyword fragments (already conceptually normalised; we normalise both
# sides at match time). Checked in this order (most urgent first).
# NOTE: fragments are short stems so they survive Whisper's spelling drift.
_KEYWORDS: list[tuple[Intent, list[str]]] = [
    # ── EMERGENCY ─────────────────────────────────────────────────────────────
    (Intent.EMERGENCY, [
        # English
        "emergency", "cant breathe", "cannot breathe", "not able to breathe",
        "trouble breathing", "hard to breathe", "chest pain", "heart attack",
        "choking", "choke", "unconscious", "collapsed", "bleeding badly", "seizure",
        # Hindi (normalised Devanagari — diacritics stripped)
        "सस नह", "सास नह", "सन नह", "दम घट", "घट रह", "सन म दरद",
        "दल का दर", "बहश", "बचव", "मर रह", "आपतकल", "दरा पड",
        # Hindi romanized
        "saans nah", "saas nah", "sans nah", "dam ghut", "seene me dard",
        "bachao", "behosh",
        # Kannada
        "ಉಸಿರಾಡಲು", "ಉಸಿರು ಕಟ್ಟು", "ಎದೆ ನೋವು", "ಹೃದಯಾಘಾತ", "ಮೂರ್ಛೆ",
        "usiradalu", "usiru kattu", "ede novu",
    ]),

    # ── MEDICATION ────────────────────────────────────────────────────────────
    (Intent.MEDICATION, [
        # English
        "medicine", "medication", "medicines", "tablet", "tablets", "pill", "pills",
        "injection", "insulin", "painkiller", "pain killer", "antibiotic", "dose",
        "drug", "syrup", "drip", "prescription",
        # Hindi (stems — covers दवा/दवाई/दवाईया/दवाइयां etc. after normalisation)
        "दव", "दवई", "दवइ", "गल", "गोल", "टबलट", "इनजकशन", "सई", "दरद क दव",
        "दवा", "दवाई",
        # Hindi romanized
        "dawai", "dava", "goli", "tablet", "injection",
        # Kannada
        "ಔಷಧಿ", "ಔಷಧ", "ಮಾತ್ರೆ", "ಚುಚ್ಚುಮದ್ದು", "ಇನ್ಸುಲಿನ್",
        "aushadhi", "aushadha", "matre", "medicine beku", "medisin",
        # Kannada-in-Devanagari (Whisper cross-script)
        "मटसन", "मडसन", "औषध",
    ]),

    # ── PAIN ──────────────────────────────────────────────────────────────────
    (Intent.PAIN, [
        # English
        "pain", "hurts", "hurting", "ache", "aching", "sore", "throbbing",
        "cramp", "burning", "painful", "my leg hurts", "my back hurts",
        # Hindi (दर्द and variants)
        "दरद", "दरद ह", "पर म दरद", "पठ म दरद", "सर म दरद", "पट म दरद",
        "टग", "जलन", "अकड",
        # Hindi romanized
        "dard", "dukh raha", "peeda",
        # Kannada
        "ನೋವು", "ನೋಯು", "ನೋಯುತ್ತಿದೆ", "ತಲೆ ನೋವು",
        "novu", "noyu", "nayutti",
        # Kannada-in-Devanagari
        "नव", "नय",
    ]),

    # ── FOOD / WATER ──────────────────────────────────────────────────────────
    (Intent.FOOD_WATER, [
        # English
        "water", "hungry", "thirsty", "food", "eat", "drink", "meal", "juice",
        "breakfast", "lunch", "dinner", "snack",
        # Hindi
        "पन", "पान", "भख", "खन", "पन चह", "भख लग", "जस",
        "पानी", "भूख",
        # Hindi romanized
        "paani", "pani", "bhookh", "khana", "bhukh",
        # Kannada
        "ನೀರು", "ಹಸಿವು", "ಆಹಾರ", "ಊಟ", "ತಿನ್ನಲು", "ಕುಡಿಯಲು",
        "neeru", "hasivu", "aahara", "oota",
    ]),

    # ── MOBILITY ──────────────────────────────────────────────────────────────
    (Intent.MOBILITY, [
        # English
        "bathroom", "toilet", "restroom", "get up", "stand up", "wheelchair",
        "help me walk", "help me move", "turn over", "sit up", "bedpan", "washroom",
        # Hindi
        "बथरम", "टयलट", "शचलय", "उठन", "चलन", "वहलचयर", "करवट",
        "बाथरूम", "शौचालय",
        # Hindi romanized
        "bathroom", "toilet", "uthna", "chalna",
        # Kannada
        "ಶೌಚಾಲಯ", "ಎದ್ದೇಳ", "ನಡೆಯ", "ಗಾಲಿಕುರ್ಚಿ",
        "shauchalaya", "eddel", "nadeya",
    ]),

    # ── HYGIENE ───────────────────────────────────────────────────────────────
    (Intent.HYGIENE, [
        # English
        "clean", "wash", "bath", "shower", "change sheets", "dirty", "diaper",
        "sponge", "soiled", "brush my teeth",
        # Hindi
        "सफ", "धन", "नहन", "चदर बदल", "गद", "डयपर",
        "साफ", "चादर",
        # Hindi romanized
        "saaf", "nahana", "chaadar",
        # Kannada
        "ಸ್ವಚ್ಛ", "ಸ್ನಾನ", "ತೊಳೆ", "ಹಾಸಿಗೆ",
        "swachcha", "snaana", "tole",
    ]),

    # ── EMOTIONAL SUPPORT ─────────────────────────────────────────────────────
    (Intent.EMOTIONAL_SUPPORT, [
        # English
        "scared", "afraid", "anxious", "lonely", "depressed", "sad", "worried",
        "crying", "someone to talk", "stay with me", "frightened",
        # Hindi
        "डर", "अकल", "उदस", "घबर", "चत", "रन",
        "डरा", "अकेला",
        # Hindi romanized
        "dar lag", "akela", "udaas", "ghabra",
        # Kannada
        "ಭಯ", "ಒಂಟಿ", "ದುಃಖ", "ಆತಂಕ",
        "bhaya", "onti", "dukha",
    ]),
]


def keyword_intent(transcript: str) -> Intent | None:
    """
    Return an Intent if the transcript clearly contains keywords for it, else None.
    Checked most-urgent-first so the highest-priority match wins.
    """
    if not transcript:
        return None
    t = _normalize(transcript)
    for intent, fragments in _KEYWORDS:
        for frag in fragments:
            nf = _normalize(frag)
            if nf and nf in t:
                return intent
    return None
