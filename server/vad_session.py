"""
vad_session.py — server-side Voice Activity Detection state machine.

Two VAD backends, selected automatically at startup:

  PRIMARY   — Silero VAD v5 ONNX model (storage/models/silero_vad.onnx)
              Loaded if the file exists (downloaded by the Android app or
              placed manually).  Accurate neural VAD, same model as the app.

  FALLBACK  — RMS energy threshold.
              Used when the ONNX model is not present.  Simple but effective
              enough for a clean hospital environment.  A log warning is
              emitted so operators know to install the model.

State machine (per patient WebSocket session)
─────────────────────────────────────────────
Each VADSession instance is created fresh when a patient connects and lives
for the duration of that WebSocket connection.

States: SILENCE → SPEAKING → SILENCE

  SILENCE → SPEAKING  : ONSET_CHUNKS  consecutive VAD-positive chunks  (~96 ms)
  SPEAKING → SILENCE  : SILENCE_CHUNKS consecutive VAD-negative chunks (~800 ms)

While SPEAKING all float chunks are buffered.
On SPEAKING → SILENCE transition the buffer is returned to the caller as a
single concatenated FloatArray so it can be WAV-encoded and sent to the
pipeline.

Why these numbers:
  ONSET_CHUNKS  = 3   — 3 × 32 ms = 96 ms sustained speech before triggering.
                        Filters out single transient noises (coughs, clicks).
  SILENCE_CHUNKS = 25 — 25 × 32 ms = 800 ms trailing silence before ending.
                        Bridges natural within-sentence pauses and breath groups
                        without cutting the patient off mid-sentence.

Chunk size: 512 samples @ 16 kHz = 32 ms exactly (Silero VAD's native window).
"""

import logging
import struct
from pathlib import Path

import numpy as np

from config import settings

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
CHUNK_SAMPLES   = 512          # 32 ms @ 16 kHz
SAMPLE_RATE     = 16_000
ONSET_CHUNKS    = 3            # consecutive positives to enter SPEAKING
SILENCE_CHUNKS  = 25           # consecutive negatives to end utterance
ENERGY_THRESH   = 0.01         # fallback RMS threshold (0–1 scale)
VAD_THRESHOLD   = 0.5          # Silero probability threshold


# ══════════════════════════════════════════════════════════════════════════════
# Backend: Silero ONNX VAD
# ══════════════════════════════════════════════════════════════════════════════

class _SileroBackend:
    """
    Thin wrapper around the Silero VAD v5 ONNX model.
    Identical logic to the Android SileroVAD.kt — LSTM state persists across
    chunks within an utterance and is reset between utterances.
    """

    def __init__(self, model_path: str):
        import onnxruntime as ort   # imported lazily — not in requirements until Layer 3
        self._env     = ort.InferenceSession(model_path)
        self._h       = np.zeros((2, 1, 64), dtype=np.float32)
        self._c       = np.zeros((2, 1, 64), dtype=np.float32)
        logger.info("Silero VAD ONNX backend loaded from %s", model_path)

    def is_speech(self, chunk: np.ndarray) -> bool:
        """chunk: float32 array of exactly 512 samples, values in [-1, 1]."""
        try:
            inputs = {
                "input": chunk.reshape(1, 512).astype(np.float32),
                "sr":    np.array([SAMPLE_RATE], dtype=np.int64),
                "h":     self._h,
                "c":     self._c,
            }
            out = self._env.run(["output", "hn", "cn"], inputs)
            prob, hn, cn = out[0][0][0], out[1], out[2]
            # Update LSTM state — NEVER skip this; zeroing between chunks
            # destroys temporal context and breaks detection.
            self._h = hn
            self._c = cn
            return float(prob) > VAD_THRESHOLD
        except Exception as e:
            logger.error("Silero inference error: %s", e)
            return False

    def reset_state(self):
        """Zero LSTM state — call between utterances, NOT between chunks."""
        self._h = np.zeros((2, 1, 64), dtype=np.float32)
        self._c = np.zeros((2, 1, 64), dtype=np.float32)


# ══════════════════════════════════════════════════════════════════════════════
# Backend: RMS energy fallback
# ══════════════════════════════════════════════════════════════════════════════

class _EnergyBackend:
    """Simple RMS energy threshold — used when Silero model is not available."""

    def is_speech(self, chunk: np.ndarray) -> bool:
        rms = float(np.sqrt(np.mean(chunk ** 2)))
        return rms > ENERGY_THRESH

    def reset_state(self):
        pass   # stateless


# ══════════════════════════════════════════════════════════════════════════════
# Module-level backend singleton — created once at import time
# ══════════════════════════════════════════════════════════════════════════════

def _load_backend():
    model_path = settings.MODELS_DIR / "silero_vad.onnx"
    if model_path.exists():
        try:
            return _SileroBackend(str(model_path))
        except Exception as e:
            logger.warning("Failed to load Silero ONNX (%s) — using energy fallback", e)
    else:
        logger.warning(
            "silero_vad.onnx not found at %s — using energy-threshold VAD fallback. "
            "Download the model via the Android app or place it manually.",
            model_path,
        )
    return _EnergyBackend()


_backend = _load_backend()


# ══════════════════════════════════════════════════════════════════════════════
# Per-session state machine
# ══════════════════════════════════════════════════════════════════════════════

class VADSession:
    """
    One instance per patient WebSocket connection.
    Call process_chunk() for every 512-sample binary frame received.
    It returns a completed utterance (FloatArray) when one is detected,
    or None while still accumulating.
    """

    def __init__(self, patient_id: str):
        self.patient_id     = patient_id
        self._speaking      = False
        self._onset_count   = 0    # consecutive positive chunks while SILENCE
        self._silence_count = 0    # consecutive negative chunks while SPEAKING
        self._buffer: list[np.ndarray] = []
        _backend.reset_state()     # clean LSTM state for this new session
        logger.debug("VADSession created for patient %s", patient_id)

    # ── Public API ────────────────────────────────────────────────────────────

    def process_chunk(self, raw_bytes: bytes) -> np.ndarray | None:
        """
        Accept one binary WebSocket frame (1024 bytes = 512 float32 samples).
        Returns a concatenated float32 utterance array when an utterance ends,
        otherwise returns None.

        raw_bytes must be exactly CHUNK_SAMPLES × 4 bytes of little-endian
        float32 PCM data, values in [-1.0, 1.0].
        """
        # Deserialise binary frame → float32 numpy array
        expected = CHUNK_SAMPLES * 4   # 4 bytes per float32
        if len(raw_bytes) != expected:
            logger.warning(
                "VAD chunk size mismatch: got %d bytes, expected %d",
                len(raw_bytes), expected,
            )
            return None

        chunk = np.frombuffer(raw_bytes, dtype="<f4").copy()  # little-endian float32

        is_speech = _backend.is_speech(chunk)

        if not self._speaking:
            # ── SILENCE state ─────────────────────────────────────────────────
            if is_speech:
                self._onset_count += 1
                # Buffer onset chunks so the first syllable is not lost
                self._buffer.append(chunk)

                if self._onset_count >= ONSET_CHUNKS:
                    # SILENCE → SPEAKING
                    self._speaking      = True
                    self._silence_count = 0
                    logger.debug(
                        "[%s] SILENCE→SPEAKING after %d onset chunks",
                        self.patient_id, ONSET_CHUNKS,
                    )
            else:
                # Non-consecutive positive — discard tentative onset buffer
                if self._onset_count > 0:
                    self._buffer.clear()
                    self._onset_count = 0
        else:
            # ── SPEAKING state ────────────────────────────────────────────────
            self._buffer.append(chunk)

            if not is_speech:
                self._silence_count += 1

                if self._silence_count >= SILENCE_CHUNKS:
                    # SPEAKING → SILENCE — utterance complete
                    logger.debug(
                        "[%s] SPEAKING→SILENCE after %d silence chunks — utterance ready (%d chunks)",
                        self.patient_id, SILENCE_CHUNKS, len(self._buffer),
                    )
                    utterance = np.concatenate(self._buffer)
                    self._buffer        = []
                    self._speaking      = False
                    self._onset_count   = 0
                    self._silence_count = 0
                    _backend.reset_state()   # reset LSTM between utterances
                    return utterance
            else:
                # Still speaking — reset the silence run counter
                self._silence_count = 0

        return None

    def reset(self):
        """Full reset — called when the WebSocket disconnects."""
        self._speaking      = False
        self._onset_count   = 0
        self._silence_count = 0
        self._buffer        = []
        _backend.reset_state()


# ── WAV encoding (used by patient_ws.py) ─────────────────────────────────────

def float32_to_wav(samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bytes:
    """
    Encode a float32 numpy array as a 16-bit PCM WAV byte string.
    Identical to WavUtils.kt on the Android side.
    """
    pcm = (samples * 32767).clip(-32768, 32767).astype("<i2")   # signed int16 LE
    pcm_bytes   = pcm.tobytes()
    data_size   = len(pcm_bytes)
    num_channels = 1
    bits         = 16
    byte_rate    = sample_rate * num_channels * bits // 8
    block_align  = num_channels * bits // 8

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,       # chunk size = total − 8
        b"WAVE",
        b"fmt ",
        16,                   # PCM sub-chunk size
        1,                    # audio format = PCM
        num_channels,
        sample_rate,
        byte_rate,
        block_align,
        bits,
        b"data",
        data_size,
    )
    return header + pcm_bytes
