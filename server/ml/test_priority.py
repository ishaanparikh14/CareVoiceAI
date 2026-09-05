# -*- coding: utf-8 -*-
"""
test_priority.py — end-to-end priority classification test across en/hi/kn.

For each phrase: transcript -> mBERT intent -> tiered distress -> priority.
Compares against the expected Critical/Urgent/Routine label and reports accuracy.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import pipeline
import priority_engine

# (language, phrase, expected_priority)
CASES = [
    # ── CRITICAL (emergency / life-threatening) ──
    ("en", "i cannot breathe please help me", "Critical"),
    ("en", "help i am having severe chest pain", "Critical"),
    ("en", "i think i am having a heart attack", "Critical"),
    ("hi", "मुझे सांस नहीं आ रही है मदद करो", "Critical"),
    ("hi", "मेरे सीने में बहुत दर्द हो रहा है मदद करो", "Critical"),
    ("kn", "ನನಗೆ ಉಸಿರಾಡಲು ಆಗುತ್ತಿಲ್ಲ ಸಹಾಯ ಮಾಡಿ", "Critical"),
    ("kn", "ನನಗೆ ಹೃದಯಾಘಾತ ಆಗುತ್ತಿದೆ", "Critical"),

    # ── URGENT (pain / medication) ──
    ("en", "i have a really bad pain in my knee", "Urgent"),
    ("en", "i need my painkiller tablet now", "Urgent"),
    ("hi", "मेरे घुटने में बहुत दर्द है", "Urgent"),
    ("hi", "मुझे दर्द की गोली चाहिए", "Urgent"),
    ("kn", "ನನ್ನ ತಲೆ ನೋಯುತ್ತಿದೆ", "Urgent"),
    ("kn", "ನನಗೆ ನೋವು ನಿವಾರಕ ಮಾತ್ರೆ ಬೇಕು", "Urgent"),

    # ── ROUTINE (food / info / comfort) ──
    ("en", "can i get some water i am thirsty", "Routine"),
    ("en", "what time is it now", "Routine"),
    ("en", "can you turn off the lights please", "Routine"),
    ("hi", "मुझे भूख लगी है कुछ खाने को दो", "Routine"),
    ("hi", "अभी क्या समय हुआ है", "Routine"),
    ("kn", "ದಯವಿಟ್ಟು ದೀಪ ಆರಿಸಿ", "Routine"),
    ("kn", "ಈಗ ಸಮಯ ಎಷ್ಟು", "Routine"),

    # ── Extra edge cases ──
    # Emergency phrasings without the word "emergency"
    ("en", "i am choking i cannot breathe", "Critical"),
    ("hi", "मेरा दम घुट रहा है", "Critical"),
    ("kn", "ನನ್ನ ಉಸಿರು ಕಟ್ಟುತ್ತಿದೆ ಬೇಗ ಬನ್ನಿ", "Critical"),
    # Mobility with mild distress → Urgent-ish but should stay Urgent only if distress; else routine-care
    ("en", "i need help getting to the bathroom", "Routine"),
    ("hi", "मुझे बाथरूम जाने में मदद चाहिए", "Routine"),
    # Emotional support (routine unless distress high)
    ("en", "i feel a little lonely today", "Routine"),
    ("kn", "ನನಗೆ ಸ್ವಲ್ಪ ಒಂಟಿತನ ಅನಿಸುತ್ತಿದೆ", "Routine"),
    # Hygiene routine
    ("hi", "मेरी चादर गंदी है इसे बदल दीजिए", "Routine"),
]


def main():
    lines = []
    correct = 0
    per_lang = {"en": [0, 0], "hi": [0, 0], "kn": [0, 0]}  # [correct, total]

    for lang, phrase, expected in CASES:
        intent = pipeline._classify_intent(phrase)
        priority = priority_engine.compute_priority(intent, phrase)
        ok = priority.value == expected
        correct += int(ok)
        per_lang[lang][0] += int(ok)
        per_lang[lang][1] += 1
        mark = "OK " if ok else "XX "
        lines.append(f"{mark}[{lang}] exp={expected:8s} got={priority.value:8s} "
                     f"intent={intent.value:18s} | {phrase[:40]}")

    lines.append("")
    lines.append(f"OVERALL: {correct}/{len(CASES)} = {correct/len(CASES):.1%}")
    for lang in ["en", "hi", "kn"]:
        c, t = per_lang[lang]
        lines.append(f"  {lang}: {c}/{t} = {c/t:.1%}")

    report = "\n".join(lines)
    (Path(__file__).parent / "priority_test_result.txt").write_text(report, encoding="utf-8")
    print("done")


if __name__ == "__main__":
    main()
