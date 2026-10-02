"""
nlp_summary.py — deterministic request summarisation for nurse TTS.

The priority decision is separate from this module. This module only extracts
the most useful request cue from the transcript so the nurse hears a short,
actionable summary. It supports English and Hindi without an external API.
"""
from __future__ import annotations

import re
import unicodedata

from models import Intent


def _norm(text: str) -> str:
    t = (text or "").lower().strip()
    d = unicodedata.normalize("NFD", t)
    d = "".join(c for c in d if unicodedata.category(c) not in ("Mn", "Mc"))
    return unicodedata.normalize("NFC", d)


_EN = [
    ("breathing", ["can't breathe", "cannot breathe", "difficulty breathing", "trouble breathing", "shortness of breath", "gasping", "choking"]),
    ("chest pain", ["chest pain", "pain in my chest", "chest hurts"]),
    ("fall", ["fell", "falling", "i fell", "fall down"]),
    ("bleeding", ["bleeding", "blood", "bleed"]),
    ("severe pain", ["severe pain", "unbearable pain", "very bad pain"]),
    ("pain", ["pain", "hurts", "hurting", "ache", "cramp", "burning"]),
    ("medication", ["medicine", "medication", "tablet", "pill", "injection", "dose", "painkiller"]),
    ("food or water", ["water", "hungry", "food", "eat", "drink", "meal", "juice"]),
    ("bathroom assistance", ["bathroom", "toilet", "restroom", "washroom"]),
    ("mobility assistance", ["help me walk", "help me move", "get up", "stand up", "wheelchair"]),
    ("hygiene assistance", ["bath", "shower", "wash", "clean", "change sheets"]),
]

_HI = [
    ("सांस लेने में दिक्कत", ["सास नह", "सांस नह", "साँस नह", "दम घुट", "सांस लेने में दिक्कत"]),
    ("सीने में दर्द", ["सीने में दर्द", "सीने म दर्द", "छाती में दर्द"]),
    ("गिरने की शिकायत", ["गिर गया", "गिर गई", "गिरने", "गिर पड़ा", "गिर पड़ी"]),
    ("खून बह रहा है", ["खून", "रक्त", "खून बह"]),
    ("तेज दर्द", ["बहुत दर्द", "तेज दर्द", "असहनीय दर्द"]),
    ("दर्द", ["दर्द", "दर्द हो", "दर्द है"]),
    ("दवा की जरूरत", ["दवा", "दवाई", "गोली", "इंजेक्शन", "दर्द की दवा"]),
    ("खाने या पानी की जरूरत", ["पानी", "भूख", "खाना", "खाने", "पीने"]),
    ("बाथरूम में सहायता", ["बाथरूम", "शौचालय", "टॉयलेट"]),
    ("चलने में सहायता", ["चलने", "उठने", "चल नहीं", "व्हीलचेयर"]),
    ("सफाई में सहायता", ["नहाने", "साफ", "चादर"]),
]


def summarize_request(transcript: str, intent: Intent | str, language: str = "en") -> str:
    """Return one short nurse-facing sentence, preserving the detected language."""
    text = (transcript or "").strip()
    if text.startswith("[MANUAL CALL]"):
        return (
            "The patient pressed the HELP button and is requesting immediate assistance."
            if language != "hi"
            else "मरीज ने HELP बटन दबाया है और तुरंत सहायता की जरूरत है।"
        )

    norm = _norm(text)
    if language == "hi":
        for label, cues in _HI:
            if any(_norm(cue) in norm for cue in cues):
                return f"मरीज को {label} की शिकायत है।"
        return f"मरीज का अनुरोध: {text[:120]}"

    for label, cues in _EN:
        if any(cue in norm for cue in cues):
            return f"The patient reports {label}."
    if intent == Intent.MEDICATION or str(intent) == Intent.MEDICATION.value:
        return "The patient has a medication-related request."
    if intent == Intent.PAIN or str(intent) == Intent.PAIN.value:
        return "The patient reports pain."
    if intent == Intent.FOOD_WATER or str(intent) == Intent.FOOD_WATER.value:
        return "The patient needs food or water assistance."
    return f"The patient's request is: {text[:120]}"
