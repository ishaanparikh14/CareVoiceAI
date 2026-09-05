"""
intent_labels.py — the 9-category intent taxonomy (CareVoice AI architecture, Layer 3b)
and the mapping from the Kaggle Medical Speech dataset's symptom prompts to those
9 nurse-call intent categories.

The Kaggle "Medical Speech, Transcription, and Intent" dataset labels each clip
with a symptom/phrase (e.g. "Knee pain", "Emotional pain", "Hard to breath").
Those ~25 symptom labels are collapsed into the 9 operational intents the
CareVoice system routes on.
"""

# The 9 intents, in a fixed order — index == class id used by the model.
INTENTS = [
    "Emergency",
    "Pain",
    "Medication",
    "Food/Water",
    "Mobility",
    "Hygiene",
    "Emotional Support",
    "Information",
    "Other",
]

LABEL2ID = {name: i for i, name in enumerate(INTENTS)}
ID2LABEL = {i: name for i, name in enumerate(INTENTS)}


# Kaggle symptom label (lowercased) → CareVoice intent.
# Symptoms that are life-threatening map to Emergency; painful conditions to Pain;
# everything acoustic/emotional to Emotional Support; the rest to sensible buckets.
SYMPTOM_TO_INTENT = {
    # ── Emergency (life-threatening / acute) ──
    "hard to breath":            "Emergency",
    "hard to breathe":           "Emergency",
    "difficulty breathing":      "Emergency",
    "shortness of breath":       "Emergency",
    "heart hurts":               "Emergency",
    "chest pain":                "Emergency",
    "feeling dizzy":             "Emergency",
    "fainting":                  "Emergency",
    "bleeding":                  "Emergency",

    # ── Pain ──
    "knee pain":                 "Pain",
    "back pain":                 "Pain",
    "joint pain":                "Pain",
    "neck pain":                 "Pain",
    "shoulder pain":             "Pain",
    "head ache":                 "Pain",
    "headache":                  "Pain",
    "stomach ache":              "Pain",
    "ear ache":                  "Pain",
    "muscle pain":               "Pain",
    "body feels weak":           "Pain",
    "foot ache":                 "Pain",
    "injury from sports":        "Pain",
    "internal pain":             "Pain",

    # ── Medication ──
    "infected wound":            "Medication",
    "open wound":                "Medication",
    "cough":                     "Medication",
    "acne":                      "Medication",
    "skin issue":                "Medication",
    "blurry vision":             "Medication",
    "hair falling out":          "Medication",

    # ── Emotional Support ──
    "emotional pain":            "Emotional Support",
    "feeling cold":              "Emotional Support",
    "feeling scared":            "Emotional Support",

    # ── Other ──
    "cannot sleep":              "Other",
    "not sleeping":              "Other",
}


def map_symptom_to_intent(symptom: str) -> str:
    """Map a raw Kaggle symptom label to one of the 9 intents. Falls back to Other."""
    key = (symptom or "").strip().lower()
    return SYMPTOM_TO_INTENT.get(key, "Other")
