# -*- coding: utf-8 -*-
"""End-to-end test of the multilingual intent classifier wired into pipeline.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import pipeline

TESTS = [
    # English
    ("en", "i have a really bad pain in my knee"),
    ("en", "i cannot breathe please help me"),
    ("en", "can i get some water i am thirsty"),
    # Hindi
    ("hi", "मुझे सांस नहीं आ रही है मदद करो"),          # Emergency
    ("hi", "मेरे घुटने में बहुत दर्द है"),                # Pain
    ("hi", "मुझे भूख लगी है कुछ खाने को दो"),            # Food/Water
    ("hi", "मुझे बाथरूम जाने में मदद चाहिए"),            # Mobility
    # Kannada
    ("kn", "ನನಗೆ ಉಸಿರಾಡಲು ಆಗುತ್ತಿಲ್ಲ ಸಹಾಯ ಮಾಡಿ"),      # Emergency
    ("kn", "ನನ್ನ ತಲೆ ನೋಯುತ್ತಿದೆ"),                      # Pain
    ("kn", "ನನಗೆ ಔಷಧಿ ಬೇಕು"),                          # Medication
]

lines = []
for lang, text in TESTS:
    intent = pipeline._classify_intent(text)
    distress = pipeline._score_distress(text)
    lines.append(f"[{lang}] {intent.value:18s} distress={distress:.2f}  <- {text}")

(Path(__file__).parent / "ml_pipeline_test.txt").write_text("\n".join(lines), encoding="utf-8")
print("done")
