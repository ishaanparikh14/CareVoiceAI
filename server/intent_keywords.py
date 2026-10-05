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
        # Kannada spelling variants Whisper 'medium' emits (empirical)
        "ಸಿರಾಡ", "ಉಸಿರಾಡ", "ಉಸಿರ", "ಎದೆ ನುವು", "ಏದೆ ನುವು", "ಎದೆನುವು",
        # Kannada→Devanagari transliterations Whisper 'small' emits (empirical)
        "उसिरो कटु", "उसिरु कटु", "एदे नोउ", "एदे नोव",
        # German (umlaut-folded; ss-form only, no esszett)
        "keine luft", "keine luft bekomm", "ersticke", "atemnot",
        "brustschmerz", "herzinfarkt", "herzanfall", "bewusstlos",
        "ohnmacht", "starke blutung", "blutet stark", "krampf anfall",
        "notfall",
    ]),

    # ── MEDICATION ────────────────────────────────────────────────────────────
    (Intent.MEDICATION, [
        # English
        "medicine", "medication", "medicines", "tablet", "tablets", "pill", "pills",
        "injection", "insulin", "painkiller", "pain killer", "antibiotic", "dose",
        "drug", "syrup", "drip", "prescription",
        # Hindi (stems — covers दवा/दवाई/दवाईया/दवाइयां etc. after normalisation)
        "दव", "दवई", "दवइ", "गोली", "गोलि", "टबलट", "इनजकशन", "दरद क दव",
        "दवा", "दवाई",
        # Hindi romanized
        "dawai", "dava", "goli", "tablet", "injection",
        # Kannada
        "ಔಷಧಿ", "ಔಷಧ", "ಮಾತ್ರೆ", "ಚುಚ್ಚುಮದ್ದು", "ಇನ್ಸುಲಿನ್",
        "aushadhi", "aushadha", "matre", "medicine beku", "medisin",
        # Kannada spelling variants Whisper 'medium' emits (empirical)
        "ಅವ್ಷಿದಿ", "ಅವ್ಷ", "ವ್ಷಿದಿ", "ಔಷಿದಿ", "ಮಾತ್ರಿ", "ಮಾತ್ರ",
        # Kannada-in-Devanagari (Whisper cross-script)
        "मटसन", "मडसन", "औषध",
        # Kannada→Devanagari transliterations Whisper 'small' emits (empirical)
        "आउश्दी", "आश्दी", "आउश", "मात्रे", "आउषधी",
        # German (umlaut-folded; ss-form only, no esszett)
        "medikament", "tablette", "spritze", "insulin", "schmerzmittel",
        "antibiotik", "tropf", "dosis", "medizin",
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
        # Kannada spelling variants Whisper 'medium' emits (empirical)
        "ನೂಯ", "ನುವಿ", "ನುವು", "ನೂವು", "ನೋಯುತ್", "ನೂಯತ್",
        # Kannada-in-Devanagari
        "नव", "नय",
        # Kannada→Devanagari transliterations Whisper 'small' emits (empirical)
        # (normalised: ो/ौ matras stripped, so नोय्→नय, नोई→नई, नोव→नव)
        "नोय", "नोई", "नोव", "नई",
        # German (umlaut-folded; ss-form only, no esszett). "weh" is NOT used
        # (3 Latin chars -> dropped by the min_len=4 Latin guard); use phrases.
        "schmerz", "schmerzt", "tut weh", "tut mir weh", "kopfschmerz",
        "ruckenschmerz", "bauchschmerz", "brennt", "krampf",
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
        # Kannada→Devanagari transliterations Whisper 'small' emits (empirical)
        "नीरु", "हसीव", "हसिव", "वागिद", "कुदियलु", "कुदिय",
        # German (umlaut-folded; ss-form only, no esszett)
        "wasser", "durst", "hunger", "essen", "trinken", "mahlzeit",
        "saft", "durstig", "hungrig",
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
        # Kannada spelling variants Whisper 'medium' emits (empirical)
        "ಶೌಚಾಲ", "ಶಾವಚಾಲ", "ಶಾವಚ", "ಚಾಲೆಕೆ", "ಗಾಲಿಕ", "ಗಾಲಿಕಾರಿ", "ಗಾಲಿಕುರ್",
        # Kannada→Devanagari transliterations Whisper 'small' emits (empirical)
        "शुव्चा", "शवचा", "शुवचा", "गालिकृ", "गालिकु", "इद्दे लल", "इद्देलल",
        # German (umlaut-folded; ss-form only, no esszett)
        "toilette", "badezimmer", "aufstehen", "rollstuhl", "umdrehen",
        "hinsetzen", "laufen helfen", "bettpfanne",
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
        # Kannada→Devanagari transliterations Whisper 'small' emits (empirical)
        "स्वट्च", "स्वच", "स्नाद", "स्ना", "हासीगे", "हासिगे",
        # German (umlaut-folded; ss-form only, no esszett)
        "waschen", "baden", "dusche", "sauber", "schmutzig", "windel",
        "bettlaken", "laken wechseln",
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
        # Kannada spelling variants Whisper 'medium' emits (empirical)
        "ವನ್ಟಿ", "ವಂಟಿ", "ಒಂಟಿತನ", "ವನ್ಟಿತನ", "ಭಯ ಆಗು",
        # Kannada→Devanagari transliterations Whisper 'small' emits (empirical)
        "वंटी", "वंटि", "ंटी तन", "भय आगु", "बे आगु",
        # German (umlaut-folded; ss-form only, no esszett)
        "angst", "allein", "einsam", "traurig", "deprimiert", "weine",
        "nervos", "besorgt", "bleib bei mir",
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
            core = nf.replace(" ", "")
            if not core:
                continue
            # Guard against ultra-short stems that match almost anything.
            # Latin fragments need >=4 chars (English words share short substrings);
            # Indic (Devanagari/Kannada) syllables carry a full mora in 2 chars,
            # so allow >=3 there — but never a single character.
            is_indic = any(0x0900 <= ord(c) <= 0x0DFF for c in core)
            min_len = 3 if is_indic else 4
            if len(core) < min_len:
                continue
            if nf in t:
                return intent
    return None
