"""
distress_analyzer.py — CareVoice acoustic + linguistic distress/urgency signal.

This is a deterministic prototype signal, not a clinical emotion diagnosis.
It combines:
  • transcript distress markers;
  • vocal energy variability;
  • pitch variability from voiced frames;
  • speech-rate signal;
  • low-energy pause ratio.

The output is a bounded 0..1 score used only as an additional triage signal.
Emergency/critical safety markers remain governed by priority_engine.py.
"""
from __future__ import annotations

import io
import logging
import math
import re
import wave

import numpy as np

logger = logging.getLogger(__name__)

# These are deliberately high-precision phrases rather than a sentiment lexicon.
_DISTRESS_PATTERNS = [
    (1.00, r"\b(?:can't|cannot|not able to)\s+breathe\b"),
    (0.90, r"\b(?:please\s+)?help\s+me\b"),
    (0.90, r"\bi(?:'m| am)\s+(?:terrified|panicking|having a panic attack)\b"),
    (0.80, r"\b(?:very|extremely|unbearable|severe)\s+(?:pain|scared|afraid|anxious|worried)\b"),
    (0.70, r"\b(?:scared|afraid|terrified|panicking|panic|anxious|anxiety|frightened|distressed)\b"),
    (0.60, r"\b(?:crying|cry|sob|sobbing|please|alone|lonely|worried|upset)\b"),
]

def _text_signal(text: str) -> float:
    t = (text or "").lower()
    hits = 0.0
    for weight, pattern in _DISTRESS_PATTERNS:
        if re.search(pattern, t):
            hits = max(hits, weight)
    return min(1.0, hits)


def _read_wav(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())

    if sample_width == 2:
        data = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    elif sample_width == 4:
        data = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"Unsupported WAV sample width: {sample_width}")

    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)

    return data, sample_rate


def _frame_features(samples: np.ndarray, sr: int) -> tuple[float, float, float, float]:
    """Return (energy_cv, pitch_cv, speech_rate_proxy, low_energy_ratio)."""
    if samples.size < max(sr // 4, 400):
        return 0.0, 0.0, 0.0, 0.0

    frame_len = max(1, int(sr * 0.025))
    hop = max(1, int(sr * 0.010))
    if samples.size < frame_len:
        return 0.0, 0.0, 0.0, 0.0

    window = np.hanning(frame_len).astype(np.float32)
    rms_values = []
    pitches = []

    # Limit work for unusually long uploads.
    max_frames = min(700, 1 + (samples.size - frame_len) // hop)

    for i in range(max_frames):
        frame = samples[i * hop:i * hop + frame_len]
        if frame.size != frame_len:
            break
        x = frame * window
        rms = float(np.sqrt(np.mean(x * x) + 1e-12))
        rms_values.append(rms)

        # Pitch estimate from autocorrelation for reasonably voiced frames.
        if rms < 0.01:
            continue

        x = x - np.mean(x)
        corr = np.correlate(x, x, mode="full")[frame_len - 1:]
        min_lag = max(1, int(sr / 350))
        max_lag = min(frame_len - 1, int(sr / 70))
        if max_lag <= min_lag:
            continue

        region = corr[min_lag:max_lag + 1]
        peak_index = int(np.argmax(region))
        peak = float(region[peak_index])
        zero = float(corr[0]) + 1e-9

        # Reject weak/non-periodic frames.
        if peak / zero >= 0.30:
            lag = min_lag + peak_index
            if lag > 0:
                pitches.append(sr / lag)

    rms = np.asarray(rms_values, dtype=np.float32)
    if rms.size == 0:
        return 0.0, 0.0, 0.0, 0.0

    mean_rms = float(np.mean(rms)) + 1e-6
    energy_cv = float(np.std(rms) / mean_rms)

    # Robustly cap pitch variability so one bad pitch estimate cannot dominate.
    pitch_cv = 0.0
    if len(pitches) >= 3:
        p = np.asarray(pitches, dtype=np.float32)
        p = p[(p >= 70) & (p <= 350)]
        if p.size >= 3:
            pitch_cv = float(np.std(p) / (np.mean(p) + 1e-6))

    # This is a speech-rate proxy. It intentionally stays weak because
    # transcript word count is incorporated separately.
    duration = samples.size / float(sr)
    zcr = np.mean(np.abs(np.diff(np.signbit(samples).astype(np.int8))))
    speech_rate_proxy = float(np.clip(zcr * 3.0, 0.0, 1.0))

    low_energy_ratio = float(np.mean(rms < max(0.01, mean_rms * 0.35)))

    return energy_cv, pitch_cv, speech_rate_proxy, low_energy_ratio


def analyze_distress(wav_bytes: bytes, transcript: str = "") -> tuple[float, dict]:
    """
    Return (score, features).

    Score is a triage signal:
      <0.35  low/neutral signal
      0.35–0.55 elevated
      0.55–0.80 high
      >=0.80 very high

    It must not be presented as a diagnosis of emotion or mental state.
    """
    try:
        samples, sr = _read_wav(wav_bytes)
        duration = max(samples.size / float(sr), 0.1)

        energy_cv, pitch_cv, rate_proxy, pause_ratio = _frame_features(samples, sr)

        words = len(re.findall(r"\S+", transcript or ""))
        words_per_min = words / duration * 60.0
        rate_signal = float(np.clip((words_per_min - 120.0) / 100.0, 0.0, 1.0))

        # Normalize acoustic signals conservatively.
        energy_signal = float(np.clip(energy_cv / 1.5, 0.0, 1.0))
        pitch_signal = float(np.clip(pitch_cv / 0.45, 0.0, 1.0))

        acoustic = (
            0.35 * energy_signal
            + 0.25 * pitch_signal
            + 0.20 * rate_proxy
            + 0.20 * pause_ratio
        )

        linguistic = _text_signal(transcript)

        # Linguistic markers are stronger because they are easier to validate
        # deterministically; acoustics provide the requested tone/behaviour cue.
        score = float(np.clip(0.70 * linguistic + 0.30 * acoustic, 0.0, 1.0))

        features = {
            "duration_seconds": round(duration, 3),
            "words_per_minute": round(words_per_min, 1),
            "energy_variability": round(energy_cv, 4),
            "pitch_variability": round(pitch_cv, 4),
            "low_energy_ratio": round(pause_ratio, 4),
            "linguistic_distress": round(linguistic, 4),
            "acoustic_distress": round(acoustic, 4),
        }

        logger.info("[DISTRESS] score=%.3f features=%s", score, features)
        return score, features

    except Exception as exc:
        # Fail closed: acoustic analysis must never break the nurse-call path.
        logger.warning("[DISTRESS] analysis unavailable: %s", exc)
        return 0.0, {"analysis_error": str(exc)}
