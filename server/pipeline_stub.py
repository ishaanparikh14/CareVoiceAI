"""
pipeline_stub.py — placeholder AI pipeline for Layer 2 development and testing.

When config.USE_STUB is True (default), the audio router calls process_audio()
from this module instead of the real Layer 3 pipeline.  The stub:

  • Accepts the raw WAV bytes so the full request path is exercised end-to-end.
  • Reads the WAV header to extract duration (a trivially-cheap operation that
    proves the audio bytes arrived intact without loading any ML model).
  • Returns a deterministic PipelineResult so the priority engine, database
    insert, and WebSocket broadcast can all be tested without GPU hardware.

Replacing with real models (Layer 3):
  1. Create pipeline.py with the same process_audio() signature.
  2. Set USE_STUB=false in .env or environment.
  3. The audio router (routers/audio.py) will automatically switch.

Stub behaviour is intentionally transparent: the IngestResponse includes
  is_stub=True  so clients and logs can distinguish real from stub results.
"""

import logging
import struct
import asyncio

from models import Intent, PipelineResult, Priority
from nlp_summary import summarize_request

logger = logging.getLogger(__name__)

# Cycles through all 9 intents in order so testers can see every priority level
# without crafting specific audio. The cycle is shared across all rooms.
_DEMO_INTENTS = list(Intent)
_demo_counter = 0


async def process_audio(wav_bytes: bytes, room_id: str) -> PipelineResult:
    """
    Stub implementation of the AI processing pipeline.

    Parameters
    ----------
    wav_bytes : Raw bytes of the received .wav file (44-byte header + PCM data).
    room_id   : The ward room that submitted the audio (used for logging only).

    Returns
    -------
    PipelineResult with placeholder values and is_stub=True.
    """
    global _demo_counter

    # ── Parse WAV header for basic validation ─────────────────────────────────
    duration_sec = _parse_wav_duration(wav_bytes)

    # ── Energy check — reject silent clips ────────────────────────────────────
    # If the PCM data is all near-zero (mic muted, no real speech) suppress it.
    if len(wav_bytes) > 44:
        import struct, math
        pcm_data = wav_bytes[44:]
        num_samples = len(pcm_data) // 2
        if num_samples > 0:
            samples = struct.unpack_from(f"<{num_samples}h", pcm_data)
            rms = math.sqrt(sum(s * s for s in samples) / num_samples) / 32768.0
            if rms < 0.005:
                logger.info("[STUB] Clip RMS=%.5f — silence, suppressing alert", rms)
                return PipelineResult(
                    transcript     = "[silence]",
                    language       = "en",
                    nlp_summary    = "No speech was detected.",
                    intent         = Intent.OTHER,
                    distress_score = 0.0,
                    priority       = Priority.ROUTINE,
                    should_alert   = False,
                    is_stub        = True,
                )
    logger.info(
        "[STUB] Received audio from room=%s | size=%d bytes | duration=%.2fs",
        room_id, len(wav_bytes), duration_sec,
    )

    # ── Simulate processing latency (remove when real models are integrated) ──
    # 150 ms mimics a realistic CPU-only faster-whisper inference on a short clip.
    await asyncio.sleep(0.15)

    # ── Deterministic intent cycling ──────────────────────────────────────────
    intent = _DEMO_INTENTS[_demo_counter % len(_DEMO_INTENTS)]
    _demo_counter += 1

    # ── Priority + alert decision (intent-driven, shared engine) ──────────────
    priority     = _compute_priority(intent)
    should_alert = _should_alert_nurse(intent)

    # ── Stub transcript ───────────────────────────────────────────────────────
    transcript = (
        f"[STUB] Room {room_id} — intent={intent.value} duration={duration_sec:.1f}s"
        f"{'' if should_alert else ' [NOT ALERTED — general speech]'}"
    )

    logger.info(
        "[STUB] Result: priority=%s intent=%s should_alert=%s",
        priority.value, intent.value, should_alert,
    )

    return PipelineResult(
        transcript     = transcript,
        intent         = intent,
        language       = "en",
        nlp_summary    = summarize_request(transcript, intent, "en"),
        distress_score = 0.0,
        priority       = priority,
        should_alert   = should_alert,
        is_stub        = True,
    )


# ── Priority engine ───────────────────────────────────────────────────────────
# Delegate to the single shared Layer 4 engine so the stub and the real pipeline
# behave identically.
import priority_engine

def _compute_priority(intent: Intent) -> Priority:
    return priority_engine.compute_priority(intent)

compute_priority = _compute_priority


def _should_alert_nurse(intent: Intent) -> bool:
    return priority_engine.should_alert_nurse(intent)

should_alert_nurse = _should_alert_nurse


# ── WAV parsing ───────────────────────────────────────────────────────────────

def _parse_wav_duration(wav_bytes: bytes) -> float:
    """
    Extract audio duration from the WAV header without decoding PCM data.

    WAV header layout (bytes 0–43, little-endian):
      0– 3  "RIFF"
      4– 7  file size − 8
      8–11  "WAVE"
     12–15  "fmt "
     16–19  fmt chunk size (16 for PCM)
     20–21  audio format  (1 = PCM)
     22–23  num channels
     24–27  sample rate
     28–31  byte rate  = sampleRate × channels × bitsPerSample / 8
     32–33  block align = channels × bitsPerSample / 8
     34–35  bits per sample
     36–39  "data"
     40–43  data chunk size (bytes of PCM)

    Returns 0.0 on any parse error so callers are never interrupted by a
    malformed header.
    """
    try:
        if len(wav_bytes) < 44:
            return 0.0

        # Unpack the fields we need from the fmt sub-chunk.
        # '<' = little-endian; I = uint32; H = uint16
        num_channels, sample_rate, byte_rate = struct.unpack_from("<HII", wav_bytes, 22)
        data_size = struct.unpack_from("<I", wav_bytes, 40)[0]

        if byte_rate == 0:
            return 0.0

        return data_size / byte_rate

    except struct.error:
        logger.warning("[STUB] Could not parse WAV header — returning 0.0s duration")
        return 0.0


