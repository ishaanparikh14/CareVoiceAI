# -*- coding: utf-8 -*-
"""
augment_multilingual.py — expand the Hindi & Kannada phrase set with natural
paraphrase variants so mBERT has enough per-language signal per intent.

Strategy (label-preserving):
  • Prepend/append common polite request markers used by patients.
  • These are natural spoken variations, not synthetic noise.
Each base phrase yields several variants, multiplying hi/kn data ~4x.
"""

# Politeness / framing affixes that don't change intent.
HI_PREFIXES = ["", "नर्स ", "सुनिए ", "कृपया ", "अरे ", "बहन ", "जी "]
HI_SUFFIXES = ["", " कृपया", " जल्दी", " please"]

KN_PREFIXES = ["", "ನರ್ಸ್ ", "ದಯವಿಟ್ಟು ", "ಸ್ವಲ್ಪ ", "ಅಕ್ಕ ", " ಏನ್ರೀ "]
KN_SUFFIXES = ["", " ದಯವಿಟ್ಟು", " ಬೇಗ", " please"]


def _variants(text: str, prefixes, suffixes, max_variants=4):
    seen = []
    for p in prefixes:
        for s in suffixes:
            v = f"{p}{text}{s}".strip()
            v = " ".join(v.split())
            if v and v not in seen:
                seen.append(v)
            if len(seen) >= max_variants:
                return seen
    return seen


def expand_language(rows, lang, prefixes, suffixes, per_phrase=4):
    """rows: list of (text, intent, lang). Returns expanded list for `lang`."""
    out = []
    for text, intent, l in rows:
        if l != lang:
            continue
        for v in _variants(text, prefixes, suffixes, per_phrase):
            out.append((v, intent, lang))
    return out


def get_augmented_multilingual(base_rows):
    """
    Given the base trilingual rows, return additional augmented hi + kn rows.
    English is left as-is (already has plenty of data from Kaggle).
    """
    extra = []
    extra += expand_language(base_rows, "hi", HI_PREFIXES, HI_SUFFIXES, per_phrase=4)
    extra += expand_language(base_rows, "kn", KN_PREFIXES, KN_SUFFIXES, per_phrase=4)
    return extra
