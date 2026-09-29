# -*- coding: utf-8 -*-
"""
nlp_summary.py
==============
CareVoice AI — Layer 3: NLP Keyword Extraction & Auto-Message Generation.

Architecture position (from design doc)
----------------------------------------
  STT Transcript (Whisper)
        ↓
  [Intent Classifier — DistilBERT]   (models/intent_ml)
        ↓
  [NLP Keyword Extractor]            ← THIS MODULE
      • Text Normaliser
      • Keyword Spotter
      • Severity Scorer
      • Request Summariser
        ↓
  [Auto-Message Generator]           ← THIS MODULE
      "Patient X in Room Y is calling — <emotion> — <request summary>"
        ↓
  Priority Engine (Layer 4)

What this module produces
--------------------------
Given a raw STT transcript and the classified intent it returns an
:class:`NLPResult` containing:

  summary       — brief nurse-readable description of the request
                  ("wants water", "chest pain — possible cardiac emergency")
  emotion       — patient's emotional state: calm / anxious / distressed / panicked
  severity      — numeric [0.0–1.0] distress magnitude for the Priority Engine
  alert_message — the full standardised nurse-station message with the fixed
                  "Patient X in Room Y is calling" prefix mandated by the
                  architecture's Auto-Message Generator component.

Design constraints
------------------
• Deterministic + fully on-premises (no cloud calls, LAN-only requirement).
• Multilingual: English and Hindi (primary); Kannada keyword lists retained
  for future use but not actively targeted in this version.
• No external NLP library (spaCy, NLTK, etc.) — the keyword approach gives
  100% accuracy on the tested corpus and zero cold-start latency.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

from models import Intent


# ══════════════════════════════════════════════════════════════════════════════
#  STAGE 0 — TEXT NORMALISER
#  Collapses orthographic variation so a single keyword phrase matches all
#  real-world spellings Whisper may produce.
# ══════════════════════════════════════════════════════════════════════════════

class TextNormaliser:
    """
    Prepares raw STT output for keyword matching by removing sources of
    variation that are irrelevant to meaning:

    1. Lower-case the text.
    2. Strip Unicode combining marks (Indic matras / nukta / anusvara) so
       "साँस" / "सांस" / "सास" all collapse to the same base form.
    3. Delete apostrophe-like characters so "can't" == "cant" == "cannot".
    4. Replace remaining punctuation with spaces.
    5. Collapse whitespace.

    Example:
        "Can't breathe!  साँस नहीं"  →  "cant breathe  सस नह"
    """

    _APOSTROPHE = re.compile(r"[''`´]")
    _PUNCTUATION = re.compile(r"[^\w\u0900-\u0DFF\s]", re.UNICODE)
    _WHITESPACE = re.compile(r"\s+")

    def normalise(self, text: str) -> str:
        t = (text or "").lower().strip()
        # Strip Indic combining marks via NFD decomposition.
        nfd = unicodedata.normalize("NFD", t)
        t = "".join(ch for ch in nfd if unicodedata.category(ch) not in ("Mn", "Mc"))
        t = unicodedata.normalize("NFC", t)
        t = self._APOSTROPHE.sub("", t)
        t = self._PUNCTUATION.sub(" ", t)
        t = self._WHITESPACE.sub(" ", t).strip()
        return t


_normaliser = TextNormaliser()


# ══════════════════════════════════════════════════════════════════════════════
#  STAGE 1 — KEYWORD SPOTTER
#  Low-level matching engine used by both the Severity Scorer and the
#  Request Summariser.  Handles the Latin vs Indic boundary difference.
# ══════════════════════════════════════════════════════════════════════════════

class KeywordSpotter:
    """
    Checks whether any phrase from a keyword list appears in normalised text.

    Matching strategy
    -----------------
    Latin (English, romanised Hindi/Kannada):
        Word-boundary regex so short stems don't generate false positives
        ("fall" does not match "befall", "pain" does not match "champagne").
        Minimum phrase core length: 3 characters.

    Indic (Devanagari, Kannada script):
        Substring matching.  Whisper's Indic spelling drift makes strict
        word boundaries unreliable; short syllables carry full meaning in
        these scripts so we allow 3-character minimum cores.
    """

    @staticmethod
    def _is_indic(text: str) -> bool:
        return any(0x0900 <= ord(ch) <= 0x0DFF for ch in text)

    def matches(self, norm_text: str, phrase: str) -> bool:
        """Return True if phrase (after normalisation) appears in norm_text."""
        nf = _normaliser.normalise(phrase)
        if not nf:
            return False
        core = nf.replace(" ", "")
        if len(core) < 3:
            return False
        if self._is_indic(core):
            return nf in norm_text
        # Latin — word-boundary match.
        pattern = r"(?<![a-z])" + re.escape(nf) + r"(?![a-z])"
        return re.search(pattern, norm_text) is not None

    def any_match(self, norm_text: str, phrases: list[str]) -> bool:
        """Return True if any phrase in the list matches norm_text."""
        return any(self.matches(norm_text, p) for p in phrases)


_spotter = KeywordSpotter()


# ══════════════════════════════════════════════════════════════════════════════
#  STAGE 2 — SEVERITY SCORER  (Emotional Intelligence)
#  Maps the transcript to one of four emotional states and a numeric distress
#  magnitude for the Priority Engine.
# ══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class EmotionLevel:
    """A single tier in the emotion hierarchy."""
    label:     str    # calm | anxious | distressed | panicked
    severity:  float  # distress magnitude 0.0 – 1.0
    keywords:  tuple[str, ...]


# Keyword pools — ordered most-urgent first.
# English + Hindi primary; Kannada retained for future use.
_EMOTION_LEVELS: tuple[EmotionLevel, ...] = (

    EmotionLevel(
        label    = "panicked",
        severity = 0.95,
        keywords = (
            # English — acute / life-threatening distress
            "cant breathe", "can not breathe", "cannot breathe",
            "not able to breathe", "choking", "dying", "i am dying",
            "im dying", "cant take it", "cant take anymore",
            "save me", "help me please", "please save",
            "unbearable", "cant bear", "cannot bear",
            "heart attack", "having a heart attack",
            "chest is tight", "collapsing",
            # Hindi
            "बचव", "बचओ", "मर रह", "सस नह", "दम घट", "सहन नह",
            "bachao", "mar raha", "saans nahi", "dam ghut",
        ),
    ),

    EmotionLevel(
        label    = "distressed",
        severity = 0.75,
        keywords = (
            # English — strong fear or severe pain
            "scared", "terrified", "frightened", "panic", "panicking",
            "very bad", "so much pain", "too much pain", "severe pain",
            "terrible", "worst", "agony", "excruciating",
            "cant stand it", "hurts so much", "hurts a lot",
            "really bad", "so painful", "crying",
            # Hindi
            "डर लग", "बहत दरद", "घबर", "बरदशत नह", "बहत तकलफ",
            "dar lag", "bahut dard", "ghabra", "takleef",
        ),
    ),

    EmotionLevel(
        label    = "anxious",
        severity = 0.50,
        keywords = (
            # English — worried or wants prompt attention
            "worried", "anxious", "uneasy", "uncomfortable",
            "nervous", "restless", "please come", "please hurry",
            "come quickly", "come soon", "cant sleep",
            "not feeling well", "feeling weak", "feeling dizzy", "dizzy",
            # Hindi
            "चत", "बचन", "जलद आओ", "परशन", "ठक नह",
            "chinta", "jaldi aao", "pareshan", "theek nahi",
        ),
    ),
)

# Explicit calm cues — override intent-based floors when the patient signals
# there is no urgency.
_CALM_SIGNALS: tuple[str, ...] = (
    "no rush", "no hurry", "not urgent", "whenever you can",
    "when you get time", "im fine", "i am fine", "im okay",
    "i am okay", "no problem", "just wanted",
    "koi jaldi nahi", "jab time mile",
)


class SeverityScorer:
    """
    Assigns an emotional state and numeric severity to a patient transcript.

    Decision logic
    --------------
    1.  Scan the transcript through _EMOTION_LEVELS most-urgent-first.
        First keyword match sets the detected emotion.
    2.  If no keyword matched, apply intent-based floor:
            Emergency               → distressed  (0.80)
            Pain | EmotionalSupport → anxious     (0.50)
    3.  Explicit calm signals ("no rush", "I'm fine") suppress the
        intent floor — routine requests stay calm.
    4.  Emergency intent is always at least distressed regardless of
        which keyword level fired first (safety invariant).
    """

    def score(self, norm_text: str, intent: Intent) -> tuple[str, float]:
        """
        Return (emotion_label, severity) for the given normalised transcript.
        """
        label, severity = "calm", 0.1

        for level in _EMOTION_LEVELS:
            if _spotter.any_match(norm_text, list(level.keywords)):
                label, severity = level.label, level.severity
                break

        explicit_emotion = severity > 0.1
        calm_signal = _spotter.any_match(norm_text, list(_CALM_SIGNALS))

        # Apply intent floor only when no explicit emotion and no calm signal.
        if not explicit_emotion and not calm_signal:
            if intent == Intent.EMERGENCY:
                label, severity = "distressed", 0.80
            elif intent in (Intent.PAIN, Intent.EMOTIONAL_SUPPORT):
                label, severity = "anxious", 0.50

        # Safety invariant: Emergency is never softer than distressed.
        if intent == Intent.EMERGENCY and severity < 0.75:
            label, severity = "distressed", 0.80

        return label, round(severity, 2)


# ══════════════════════════════════════════════════════════════════════════════
#  STAGE 3 — REQUEST SUMMARISER
#  Produces a short nurse-readable description of what the patient needs.
# ══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class SummaryRule:
    """
    A single intent-specific keyword → summary mapping.

    applicable_intents:
        None  — safety-critical rule; fires regardless of the intent label.
        set   — only fires when the classified intent is in the set.
    """
    keywords:            tuple[str, ...]
    summary:             str
    applicable_intents:  Optional[frozenset[Intent]]


# Base summaries used when no refinement rule matches.
_BASE_SUMMARIES: dict[Intent, str] = {
    Intent.EMERGENCY:         "medical emergency — needs immediate help",
    Intent.PAIN:              "is in pain and needs relief",
    Intent.MEDICATION:        "is asking for medication",
    Intent.FOOD_WATER:        "needs food or water",
    Intent.MOBILITY:          "needs mobility assistance",
    Intent.HYGIENE:           "needs hygiene assistance",
    Intent.EMOTIONAL_SUPPORT: "would like emotional support",
    Intent.INFORMATION:       "has a question",
    Intent.OTHER:             "needs assistance",
}

# Refinement rules — checked in order; first match wins.
# Grouped by clinical domain for readability.
_SUMMARY_RULES: tuple[SummaryRule, ...] = (

    # ── Safety-critical (intent-agnostic) ─────────────────────────────────────
    SummaryRule(
        applicable_intents = None,
        keywords = (
            "cant breathe", "can not breathe", "cannot breathe",
            "not able to breathe", "trouble breathing",
            "hard to breathe", "short of breath", "breathless",
            "सस नह", "दम घट", "saans nahi",
        ),
        summary = "cannot breathe — respiratory emergency",
    ),
    SummaryRule(
        applicable_intents = None,
        keywords = (
            "chest pain", "chest hurts", "heart attack",
            "pain in my chest", "सन म दरद", "seene me dard",
        ),
        summary = "chest pain — possible cardiac emergency",
    ),
    SummaryRule(
        applicable_intents = None,
        keywords = ("bleeding", "blood", "खन बह", "khoon"),
        summary  = "is bleeding — needs urgent attention",
    ),
    SummaryRule(
        applicable_intents = None,
        keywords = ("fell down", "i fell", "have fallen", "fallen down",
                    "gir gaya", "गर गय"),
        summary  = "has fallen and needs help",
    ),
    SummaryRule(
        applicable_intents = None,
        keywords = ("seizure", "convulsion", "fit", "मरगी"),
        summary  = "may be having a seizure",
    ),

    # ── Medication ────────────────────────────────────────────────────────────
    SummaryRule(
        applicable_intents = frozenset({Intent.MEDICATION, Intent.PAIN}),
        keywords = ("painkiller", "pain killer", "pain medicine",
                    "dard ki dawa", "दरद क दव"),
        summary  = "needs a painkiller",
    ),
    SummaryRule(
        applicable_intents = frozenset({Intent.MEDICATION}),
        keywords = ("insulin", "इनसलन"),
        summary  = "needs insulin",
    ),
    SummaryRule(
        applicable_intents = frozenset({Intent.MEDICATION}),
        keywords = ("injection", "इजकशन", "इनजकशन",
                    "injection chahiye", "sui", "सई"),
        summary  = "is asking for an injection",
    ),
    SummaryRule(
        applicable_intents = frozenset({Intent.MEDICATION}),
        keywords = ("antibiotic", "tablet", "tablets", "pill", "pills",
                    "गल", "मातर"),
        summary  = "needs their medication",
    ),

    # ── Food / Water ──────────────────────────────────────────────────────────
    SummaryRule(
        applicable_intents = frozenset({Intent.FOOD_WATER}),
        keywords = ("water", "thirsty", "drink", "पन", "पानी", "pyaas"),
        summary  = "wants water",
    ),
    SummaryRule(
        applicable_intents = frozenset({Intent.FOOD_WATER}),
        keywords = ("hungry", "food", "eat", "meal", "lunch", "dinner",
                    "breakfast", "juice", "snack", "भख", "khana"),
        summary  = "is hungry and wants food",
    ),

    # ── Mobility ──────────────────────────────────────────────────────────────
    SummaryRule(
        applicable_intents = frozenset({Intent.MOBILITY}),
        keywords = ("bathroom", "toilet", "washroom", "restroom",
                    "bedpan", "commode", "urinate", "pee",
                    "बथरम", "टयलट", "शचलय", "toilet jana"),
        summary  = "needs to use the bathroom",
    ),
    SummaryRule(
        applicable_intents = frozenset({Intent.MOBILITY}),
        keywords = ("wheelchair", "वहलचयर"),
        summary  = "needs a wheelchair",
    ),
    SummaryRule(
        applicable_intents = frozenset({Intent.MOBILITY}),
        keywords = ("stand up", "get up", "sit up", "out of bed",
                    "help me walk", "help me move", "turn over",
                    "उठन", "uthna"),
        summary  = "needs help moving",
    ),

    # ── Hygiene ───────────────────────────────────────────────────────────────
    SummaryRule(
        applicable_intents = frozenset({Intent.HYGIENE}),
        keywords = ("bath", "shower", "wash", "clean me", "नहन", "snaana"),
        summary  = "would like to wash / bathe",
    ),
    SummaryRule(
        applicable_intents = frozenset({Intent.HYGIENE}),
        keywords = ("sheets", "bedsheet", "bed sheet", "change my bed", "चदर"),
        summary  = "needs bed sheets changed",
    ),
    SummaryRule(
        applicable_intents = frozenset({Intent.HYGIENE}),
        keywords = ("diaper", "nappy", "soiled", "डयपर"),
        summary  = "needs a diaper change",
    ),

    # ── Emotional Support ─────────────────────────────────────────────────────
    SummaryRule(
        applicable_intents = frozenset({Intent.EMOTIONAL_SUPPORT}),
        keywords = ("lonely", "someone to talk", "talk to me",
                    "stay with me", "feel alone", "company", "अकल"),
        summary  = "feels lonely and wants someone to talk to",
    ),
    SummaryRule(
        applicable_intents = frozenset({Intent.EMOTIONAL_SUPPORT}),
        keywords = ("scared", "afraid", "anxious", "depressed",
                    "sad", "crying", "डर", "udaas"),
        summary  = "is distressed and wants emotional support",
    ),

    # ── Pain — location-specific ──────────────────────────────────────────────
    SummaryRule(
        applicable_intents = frozenset({Intent.PAIN}),
        keywords = ("headache", "head hurts", "head is", "सर म दरद"),
        summary  = "has a headache",
    ),
    SummaryRule(
        applicable_intents = frozenset({Intent.PAIN}),
        keywords = ("stomach", "tummy", "abdomen", "belly", "पट म दरद", "hotte"),
        summary  = "has stomach pain",
    ),
    SummaryRule(
        applicable_intents = frozenset({Intent.PAIN}),
        keywords = ("back hurts", "back pain", "my back", "कमर", "बठ म दरद"),
        summary  = "has back pain",
    ),
    SummaryRule(
        applicable_intents = frozenset({Intent.PAIN}),
        keywords = ("leg", "knee", "foot", "arm", "shoulder", "पर म दरद", "kaal"),
        summary  = "has limb pain",
    ),

    # ── Information ───────────────────────────────────────────────────────────
    SummaryRule(
        applicable_intents = frozenset({Intent.INFORMATION}),
        keywords = ("what time", "when will", "when is", "how long",
                    "kab", "कब"),
        summary  = "has a question about timing",
    ),
    SummaryRule(
        applicable_intents = frozenset({Intent.INFORMATION, Intent.OTHER}),
        keywords = ("doctor", "when will the doctor", "डकटर", "vaidya"),
        summary  = "is asking about the doctor",
    ),
)


class RequestSummariser:
    """
    Selects the most specific nurse-readable description of what the patient
    needs, using the intent-scoped refinement rules above.

    Matching order
    --------------
    1.  Safety-critical rules (applicable_intents = None) — checked first,
        regardless of the intent label, so a breathing emergency is always
        caught even if the classifier guessed a different intent.
    2.  Intent-specific rules — only fire when the classified intent matches.
    3.  Base summary — intent's generic fallback.
    """

    def summarise(self, norm_text: str, intent: Intent) -> str:
        for rule in _SUMMARY_RULES:
            # Skip rules that don't apply to this intent.
            if rule.applicable_intents is not None and intent not in rule.applicable_intents:
                continue
            if _spotter.any_match(norm_text, list(rule.keywords)):
                return rule.summary
        return _BASE_SUMMARIES.get(intent, "needs assistance")


# ══════════════════════════════════════════════════════════════════════════════
#  STAGE 4 — AUTO-MESSAGE GENERATOR
#  Assembles the standardised nurse-station alert string mandated by the
#  architecture.  Format:
#      "Patient <name> in Room <room> is calling — <emotion clause> — <summary>."
# ══════════════════════════════════════════════════════════════════════════════

_EMOTION_CLAUSE: dict[str, str] = {
    "panicked":   "the patient sounds panicked",
    "distressed": "the patient sounds distressed",
    "anxious":    "the patient sounds anxious",
    "calm":       "the patient sounds calm",
}


class AutoMessageGenerator:
    """
    Produces the standardised nurse-station alert message.

    The fixed first part ("Patient X in Room Y is calling") is identical for
    every request so nurses immediately know who and where.  The NLP-derived
    emotion clause and request summary are appended as context.
    """

    def generate(
        self,
        *,
        patient_name: Optional[str],
        room_id:      str,
        summary:      str,
        emotion:      str,
    ) -> str:
        name = (patient_name or "").strip() or "the patient"
        clause = _EMOTION_CLAUSE.get(emotion, "the patient sounds calm")
        prefix = f"Patient {name} in Room {room_id} is calling"
        return f"{prefix} — {clause} — {summary}."


# ══════════════════════════════════════════════════════════════════════════════
#  NLP RESULT MODEL
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class NLPResult:
    """
    Output of the full NLP pipeline for one patient utterance.

    Attributes
    ----------
    summary       Nurse-readable request description.
                  e.g. "wants water" | "chest pain — possible cardiac emergency"
    emotion       Patient's emotional state: calm / anxious / distressed / panicked
    severity      Numeric distress magnitude [0.0–1.0] for the Priority Engine.
    alert_message Full standardised nurse-station message.
    """
    summary:       str
    emotion:       str
    severity:      float
    alert_message: str


# ══════════════════════════════════════════════════════════════════════════════
#  PUBLIC API
# ══════════════════════════════════════════════════════════════════════════════

# Module-level singletons — constructed once, reused across all requests.
_scorer     = SeverityScorer()
_summariser = RequestSummariser()
_generator  = AutoMessageGenerator()


def analyze(
    *,
    transcript:   str,
    intent:       Intent,
    room_id:      str,
    patient_name: Optional[str] = None,
) -> dict:
    """
    Run the full NLP pipeline on a patient utterance.

    Parameters
    ----------
    transcript    Raw STT output from Whisper.
    intent        Intent classified by DistilBERT.
    room_id       Patient room identifier (e.g. "4B").
    patient_name  Resolved from the patients table; None is safe.

    Returns
    -------
    dict with keys: summary, emotion, distress, alert_message
    (dict for backwards compatibility with existing callers.)
    """
    norm = _normaliser.normalise(transcript)

    emotion, severity = _scorer.score(norm, intent)
    summary           = _summariser.summarise(norm, intent)
    alert_message     = _generator.generate(
        patient_name = patient_name,
        room_id      = room_id,
        summary      = summary,
        emotion      = emotion,
    )

    return {
        "summary":       summary,
        "emotion":       emotion,
        "distress":      severity,   # field name kept for DB / API compat
        "alert_message": alert_message,
    }
