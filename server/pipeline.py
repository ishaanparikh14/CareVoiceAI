"""
pipeline.py — Real on-premises AI pipeline using faster-whisper for STT
and a keyword/rule-based intent + distress classifier.

No external API calls. All processing runs on your local GPU via CUDA.

Model: openai/whisper-base (loaded once at startup, reused per request)
Intent: keyword matching against the transcript
Distress: urgency-word scoring on the transcript

To use this pipeline set USE_STUB=false in .env (or environment variable).
"""

import logging
import re

from models import Intent, PipelineResult, Priority
from config import settings
import priority_engine

logger = logging.getLogger(__name__)


# ── Whisper model singleton ───────────────────────────────────────────────────

_whisper_model = None

def _register_cuda_dlls():
    """
    Add pip-installed NVIDIA cuBLAS/cuDNN DLL folders to the DLL search path so
    faster-whisper (CTranslate2) can find them on Windows. These come from the
    nvidia-cublas-cu12 / nvidia-cudnn-cu12 wheels and are NOT on PATH by default.

    On Linux (e.g. the Modal CUDA container) the CUDA libraries are already on
    the system library path and os.add_dll_directory does not exist, so we simply
    return True to allow the GPU path — no DLL registration is needed there.
    """
    import os, glob, site
    if os.name != "nt":
        # Non-Windows: assume the CUDA runtime is provided by the base image.
        return True
    try:
        registered = False
        # nvidia.* are namespace packages (__file__ may be None); resolve via
        # their __path__ entries and also scan site-packages as a fallback.
        search_roots = []
        try:
            import nvidia
            search_roots.extend(list(getattr(nvidia, "__path__", [])))
        except Exception:
            pass
        for sp in site.getsitepackages() + [site.getusersitepackages()]:
            search_roots.append(os.path.join(sp, "nvidia"))

        seen = set()
        for root in search_roots:
            if not root or root in seen or not os.path.isdir(root):
                continue
            seen.add(root)
            for bin_dir in glob.glob(os.path.join(root, "*", "bin")):
                if os.path.isdir(bin_dir):
                    os.add_dll_directory(bin_dir)
                    os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
                    registered = True
        return registered
    except Exception as exc:
        logger.warning("Could not register CUDA DLLs: %s", exc)
        return False


def _get_whisper():
    global _whisper_model
    if _whisper_model is None:
        import os
        from faster_whisper import WhisperModel

        # Model size: "medium" is required for reliable Kannada — "small" emits
        # Devanagari/garbled script for Kannada speech. "medium" stays in correct
        # Kannada script and runs in ~1-2s on GPU. Overridable via WHISPER_MODEL.
        model_size = os.environ.get("WHISPER_MODEL", "medium")

        # Try GPU first (fast + accurate). Requires cuBLAS/cuDNN — register the
        # pip-installed DLLs, then self-test with a real transcribe call so we
        # catch missing-DLL failures now instead of on the first patient request.
        want_cpu = os.environ.get("WHISPER_DEVICE", "").lower() == "cpu"
        if not want_cpu and _register_cuda_dlls():
            try:
                m = WhisperModel(model_size, device="cuda", compute_type="float16")
                import numpy as np
                list(m.transcribe(np.zeros(16000, dtype=np.float32))[0])  # force cuBLAS
                _whisper_model = m
                logger.info("Whisper '%s' loaded on CUDA (float16)", model_size)
                return _whisper_model
            except Exception as exc:
                logger.warning("CUDA Whisper unavailable (%s) — falling back to CPU", exc)

        # CPU fallback — medium/small are slow on CPU. Drop to "small" so the
        # server stays responsive; Kannada quality degrades without a GPU, which
        # is an accepted tradeoff for CPU-only deployments.
        cpu_size = "small" if model_size in ("medium", "large-v3", "large") else model_size
        _whisper_model = WhisperModel(cpu_size, device="cpu", compute_type="int8")
        logger.info("Whisper '%s' loaded on CPU (int8)", cpu_size)
    return _whisper_model


# ── Intent keyword map ────────────────────────────────────────────────────────
# Each intent maps to a list of keyword patterns (regex, case-insensitive).
# Longer / more specific patterns are listed first so they take priority.

_INTENT_PATTERNS: list[tuple[Intent, list[str]]] = [
    (Intent.EMERGENCY, [
        r"\bhelp\b.*\burgent\b", r"\bemergency\b", r"\bcan'?t breathe\b",
        r"\bchest pain\b", r"\bheart\b.*\bhurt", r"\bcall.*doctor\b",
        r"\bcode\b", r"\bfalling\b", r"\bfall(ing|en)?\b.*\bdown\b",
        r"\bdon'?t feel.*well\b",
    ]),
    (Intent.PAIN, [
        r"\bhurt(s|ing)?\b", r"\bpain\b", r"\bache\b", r"\bsore\b",
        r"\bthrobbing\b", r"\bburning\b", r"\bstinging\b", r"\bcramp\b",
        r"\bit hurts\b", r"\bpainful\b", r"\buncomfortable\b",
    ]),
    (Intent.MEDICATION, [
        r"\bmedic(ine|ation|al)?\b", r"\bpill\b", r"\bdrug\b", r"\bdose\b",
        r"\bprescri(ption|bed)\b", r"\binsulin\b", r"\bpainkiller\b",
        r"\bantibiotic\b", r"\bparacetamol\b", r"\bibuprofen\b",
        r"\binjection\b", r"\bdrip\b", r"\biv\b",
    ]),
    (Intent.FOOD_WATER, [
        r"\bhungry\b", r"\bthirsty\b", r"\bwater\b", r"\bfood\b",
        r"\beat\b", r"\bdrink\b", r"\bmeal\b", r"\blunch\b",
        r"\bbreakfast\b", r"\bdinner\b", r"\bsnack\b",
    ]),
    (Intent.MOBILITY, [
        r"\bwalk\b", r"\bstand\b", r"\bget up\b", r"\bmove\b",
        r"\bwheelchair\b", r"\bstretcher\b", r"\bcrutch\b",
        r"\btoilet\b", r"\bbathroom\b", r"\bwc\b", r"\bloo\b",
        r"\bbedpan\b", r"\bcommode\b",
    ]),
    (Intent.HYGIENE, [
        r"\bclean\b", r"\bwash\b", r"\bbath\b", r"\bshower\b",
        r"\bchange.*sheets?\b", r"\bdirty\b", r"\bsmell\b",
        r"\bsanitary\b", r"\bdiaper\b", r"\bpad\b",
    ]),
    (Intent.EMOTIONAL_SUPPORT, [
        r"\bscared\b", r"\bafraid\b", r"\bworried\b", r"\banxious\b",
        r"\bdepressed\b", r"\bsad\b", r"\bcry(ing)?\b", r"\balone\b",
        r"\blonely\b", r"\bupset\b", r"\bfrightened\b",
        r"\bneed.*nurse\b", r"\bplease.*come\b", r"\bsomebody.*help\b",
        r"\bneed.*help\b", r"\bcan.*you.*help\b",
    ]),
    (Intent.INFORMATION, [
        r"\bwhat (is|are|time|day)\b", r"\bwhen\b.*\b(will|can|do)\b",
        r"\btell me\b", r"\bexplain\b", r"\bhow (do|does|long|much)\b",
        r"\bwhy\b", r"\bwhere\b", r"\bwho\b",
    ]),
]


def _classify_intent_keywords(transcript: str) -> Intent:
    """Fallback keyword matcher — used only if the DistilBERT model isn't present."""
    t = transcript.lower()
    for intent, patterns in _INTENT_PATTERNS:
        for pat in patterns:
            if re.search(pat, t):
                return intent
    return Intent.OTHER


# ── DistilBERT intent classifier (trained — CareVoice architecture Layer 3b) ──

from pathlib import Path

# Multilingual (en/hi/kn) DistilBERT intent classifier.
_INTENT_MODEL_DIR = Path(__file__).parent / "storage" / "models" / "intent_ml"
_intent_model = None
_intent_tokenizer = None


def _get_intent_model():
    """
    Lazy-load the multilingual DistilBERT intent classifier.
    Returns (model, tokenizer) or (None, None) — the caller falls back to the
    keyword matcher if the model can't be loaded.
    """
    global _intent_model, _intent_tokenizer
    if _intent_model is not None:
        return _intent_model, _intent_tokenizer

    if _INTENT_MODEL_DIR.exists():
        try:
            import torch  # noqa: F401
            from transformers import AutoTokenizer, AutoModelForSequenceClassification
            _intent_tokenizer = AutoTokenizer.from_pretrained(str(_INTENT_MODEL_DIR))
            _intent_model = AutoModelForSequenceClassification.from_pretrained(str(_INTENT_MODEL_DIR))
            _intent_model.eval()
            logger.info("Multilingual intent classifier loaded from %s", _INTENT_MODEL_DIR)
        except Exception as exc:
            logger.warning("Could not load intent model (%s) — using keyword fallback", exc)
            _intent_model = None
    return _intent_model, _intent_tokenizer


def _classify_intent(transcript: str) -> Intent:
    """
    Classify transcript into one of the 9 intents.

    Robust two-stage approach:
      1. High-precision multilingual keyword override (intent_keywords) — if the
         transcript clearly contains words for an intent, trust that. This fixes
         the DistilBERT model's inconsistency on Whisper's Hindi/Kannada spelling
         variants (e.g. "दवाईया" → Medication, not Food/Water).
      2. Otherwise fall back to the DistilBERT model (or keyword matcher if the
         model isn't loaded).
    """
    import intent_keywords

    # Stage 1: keyword override (deterministic, multilingual, high precision)
    kw = intent_keywords.keyword_intent(transcript)

    # Stage 2: model prediction
    model, tokenizer = _get_intent_model()
    if model is None:
        model_intent = _classify_intent_keywords(transcript)
    else:
        import torch
        enc = tokenizer(transcript, truncation=True, max_length=64, return_tensors="pt")
        with torch.no_grad():
            logits = model(**enc).logits
        pred_id = int(torch.argmax(logits, dim=-1).item())
        model_intent = Intent(model.config.id2label[pred_id])

    if kw is None:
        return model_intent

    # Keyword override wins for the safety-relevant intents; for the rest we
    # still prefer the keyword hit since it's high-precision by construction.
    if kw != model_intent:
        logger.info("[INTENT] keyword override: model=%s -> keyword=%s", model_intent.value, kw.value)
    return kw


# ── Main pipeline entry point ─────────────────────────────────────────────────

async def process_audio(wav_bytes: bytes, room_id: str, lang_hint: str | None = None) -> PipelineResult:
    """
    Real pipeline:
      1. Transcribe WAV with faster-whisper (multilingual, auto-detect)
      2. Classify intent with the DistilBERT model (keyword fallback)
      3. Map intent -> priority (Critical/Urgent/Routine) with a critical-marker
         safety override; decide whether to notify the nurse.

    lang_hint: optional ISO code ('en'/'hi'/'kn'/'de'). When provided, Whisper is
    forced to that language — the most reliable path (no auto-detect ambiguity).
    """
    import asyncio

    # ── Step 1: Transcribe ────────────────────────────────────────────────────
    transcript, language = await asyncio.get_event_loop().run_in_executor(
        None, _transcribe, wav_bytes, lang_hint
    )
    logger.info("[PIPELINE] room=%s lang=%s transcript: %s", room_id, language, transcript)

    # ── Step 2: Classify intent ───────────────────────────────────────────────
    intent = _classify_intent(transcript)
    logger.info("[PIPELINE] intent: %s", intent.value)

    # ── Step 3: Priority + alert decision (intent-driven) ─────────────────────
    priority     = priority_engine.compute_priority(intent, transcript)
    should_alert = priority_engine.should_alert_nurse(intent, transcript)

    logger.info("[PIPELINE] priority=%s should_alert=%s", priority.value, should_alert)

    # ── Step 4: NLP summary + emotional intelligence ──────────────────────────
    import nlp_summary
    from pg_database import get_patient_name_by_room
    patient_name = await get_patient_name_by_room(room_id)
    nlp = nlp_summary.analyze(
        transcript=transcript, intent=intent, room_id=room_id, patient_name=patient_name
    )
    logger.info("[PIPELINE] emotion=%s summary=%s", nlp["emotion"], nlp["summary"])

    return PipelineResult(
        transcript     = transcript,
        intent         = intent,
        distress_score = nlp["distress"],   # now carries the emotion magnitude
        priority       = priority,
        language       = language,
        summary        = nlp["summary"],
        emotion        = nlp["emotion"],
        alert_message  = nlp["alert_message"],
        should_alert   = should_alert,
        is_stub        = False,
    )


# Unicode block ranges used to tell Indic scripts apart in transcripts.
_KANNADA_RANGE    = (0x0C80, 0x0CFF)
_DEVANAGARI_RANGE = (0x0900, 0x097F)

# Languages Whisper commonly emits in Devanagari script. When Kannada speech is
# misdetected it almost always lands in one of these.
_DEVANAGARI_LANGS = {"hi", "mr", "ne", "sa"}


def _script_ratio(text: str, lo: int, hi: int) -> float:
    """Fraction of letters in `text` whose codepoint falls in [lo, hi]."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    inrange = sum(1 for c in letters if lo <= ord(c) <= hi)
    return inrange / len(letters)


def _run_whisper(model, path: str, language: str | None):
    """Single transcription pass. Returns (text, info, avg_logprob)."""
    segments, info = model.transcribe(
        path,
        language   = language,   # None → auto-detect
        beam_size  = 5,
        vad_filter = False,
        condition_on_previous_text = False,
    )
    segs = list(segments)
    text = " ".join(s.text.strip() for s in segs).strip()
    # Average segment log-prob is Whisper's confidence proxy; higher = better.
    if segs:
        avg_lp = sum(getattr(s, "avg_logprob", -5.0) for s in segs) / len(segs)
    else:
        avg_lp = -10.0
    return text, info, avg_lp


def _transcribe(wav_bytes: bytes, lang_hint: str | None = None) -> tuple[str, str]:
    """
    Run faster-whisper transcription synchronously (called via executor).

    Strategy (robust multilingual, en/hi/kn/de):
      • If lang_hint is given ('en'/'hi'/'kn'/'de'), force that language — most
        reliable. A forced 'de' returns before the Indic correction block, so
        German is never rewritten to a Devanagari language.
      • Otherwise auto-detect, then correct two known failure modes:
          - Urdu detected  → re-transcribe as Hindi (Devanagari).
          - Kannada speech mis-emitted as Devanagari → re-transcribe as Kannada
            and keep whichever pass is more confident + script-consistent.
    Returns (transcript, language_code).
    """
    import tempfile, os
    model = _get_whisper()

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(wav_bytes)
        tmp_path = tmp.name

    try:
        # ── Forced language (explicit hint) — no ambiguity, best accuracy ──────
        # A forced "de" (like en/hi/kn) is passed straight to Whisper and
        # RETURNS here, before the Indic auto-detect/correction block below, so
        # German is never rewritten to hi/kn. For this reason "de" must NOT be
        # added to _DEVANAGARI_LANGS (that block runs only on the auto-detect
        # branch where lang_hint is None/blank).
        if lang_hint in ("en", "hi", "kn", "de"):
            text, info, _ = _run_whisper(model, tmp_path, lang_hint)
            logger.info("Whisper(forced=%s): text='%s'", lang_hint, text)
            return (text if text else "[silence]", lang_hint)

        # ── Auto-detect pass ──────────────────────────────────────────────────
        text, info, lp_auto = _run_whisper(model, tmp_path, None)
        lang = getattr(info, "language", "unknown")

        # Fix 1: Urdu → Hindi (same spoken language, different script).
        if lang == "ur":
            text, info, _ = _run_whisper(model, tmp_path, "hi")
            lang = "hi"
            logger.info("Urdu detected → re-transcribed as Hindi (Devanagari)")

        # Fix 2: Kannada mis-detected as a Devanagari language. This is the main
        # end-to-end Kannada bug: Kannada speech gets emitted in Devanagari, so
        # neither the display nor the Kannada keyword layer works. When the auto
        # pass is Devanagari-Indic, run a forced Kannada pass and compare.
        if lang in _DEVANAGARI_LANGS:
            kn_text, _, lp_kn = _run_whisper(model, tmp_path, "kn")
            kn_ratio = _script_ratio(kn_text, *_KANNADA_RANGE)
            # Prefer Kannada when the forced pass actually yields Kannada script
            # and is at least as confident as the Devanagari auto pass. The small
            # margin (0.5) tolerates Whisper's slightly lower LP on forced runs.
            if kn_ratio >= 0.5 and lp_kn >= lp_auto - 0.5:
                logger.info(
                    "Kannada correction: auto=%s(lp=%.2f) → kn(lp=%.2f, script=%.0f%%) '%s'",
                    lang, lp_auto, lp_kn, kn_ratio * 100, kn_text,
                )
                text, lang = kn_text, "kn"

        logger.info("Whisper: lang=%s text='%s'", lang, text)
        return (text if text else "[silence]", lang)
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
