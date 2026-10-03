package com.carevoice.app

import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.sqrt

/**
 * WavUtils — converts a normalised Float32 audio buffer into a complete,
 * self-contained WAV (RIFF/PCM) byte array ready to be POSTed to the
 * FastAPI /audio/ingest endpoint.
 *
 * The output format is:
 *   • Container : RIFF / WAVE
 *   • Encoding  : PCM, signed 16-bit, little-endian
 *   • Channels  : 1 (mono)
 *   • Sample rate: 16 000 Hz (matches VAD and faster-whisper expectations)
 */
object WavUtils {

    /**
     * Computes RMS energy of a float PCM buffer, clamped to [0, 1].
     * Used by AudioRecorder, MainViewModel, and SileroVAD.
     */
    fun computeRms(samples: FloatArray): Float {
        if (samples.isEmpty()) return 0f
        return sqrt(samples.fold(0.0) { acc, s -> acc + s * s } / samples.size)
            .toFloat().coerceIn(0f, 1f)
    }

    /**
     * Encodes [floatSamples] as a 16-bit PCM WAV and prepends the standard
     * 44-byte RIFF header.
     *
     * @param floatSamples  Normalised audio samples in [-1.0, 1.0].  These are
     *                      the concatenated float chunks produced by AudioRecorder.
     * @param sampleRate    Samples per second.  Must match the VAD sample rate
     *                      (16 000 Hz).  Exposed as a parameter so unit tests
     *                      can supply alternative values.
     * @return              Complete WAV file as a ByteArray, ready to POST.
     */
    fun toWavByteArray(
        floatSamples: FloatArray,
        sampleRate: Int = 16_000
    ): ByteArray {

        val numChannels    = 1
        val bitsPerSample  = 16
        val bytesPerSample = bitsPerSample / 8                        // = 2

        // Size of the raw PCM payload in bytes.
        val pcmDataSize    = floatSamples.size * bytesPerSample       // samples × 2

        // Total file size = 44-byte header + PCM data.
        // The RIFF chunk size field stores (total − 8) because it excludes the
        // "RIFF" fourCC and the 4-byte chunk-size field itself.
        val riffChunkSize  = 36 + pcmDataSize                        // 44 − 8 + pcmDataSize

        val byteRate       = sampleRate * numChannels * bytesPerSample // bytes delivered per second
        val blockAlign     = numChannels * bytesPerSample              // bytes per sample frame

        // Allocate the full output buffer: 44-byte header + PCM data.
        val buffer = ByteBuffer
            .allocate(44 + pcmDataSize)
            .order(ByteOrder.LITTLE_ENDIAN)  // WAV is little-endian throughout

        // ── RIFF chunk descriptor ─────────────────────────────────────────────

        // Bytes 0–3: "RIFF" — chunk ID identifying this as a RIFF file.
        buffer.put('R'.code.toByte())
        buffer.put('I'.code.toByte())
        buffer.put('F'.code.toByte())
        buffer.put('F'.code.toByte())

        // Bytes 4–7: Chunk size = total file size − 8 bytes.
        //   Excludes the "RIFF" fourCC (4 bytes) and this size field (4 bytes).
        //   Value = 36 + pcmDataSize.
        buffer.putInt(riffChunkSize)

        // Bytes 8–11: "WAVE" — format identifier; distinguishes WAVE from other
        //   RIFF sub-formats (e.g. AVI).
        buffer.put('W'.code.toByte())
        buffer.put('A'.code.toByte())
        buffer.put('V'.code.toByte())
        buffer.put('E'.code.toByte())

        // ── fmt  sub-chunk ────────────────────────────────────────────────────

        // Bytes 12–15: "fmt " — sub-chunk ID (note the trailing space).
        buffer.put('f'.code.toByte())
        buffer.put('m'.code.toByte())
        buffer.put('t'.code.toByte())
        buffer.put(' '.code.toByte())

        // Bytes 16–19: Sub-chunk size = 16 for PCM (no extension fields).
        //   If audio format were non-PCM this would be 18 or 40.
        buffer.putInt(16)

        // Bytes 20–21: Audio format = 1 (PCM / linear quantisation).
        //   Any value other than 1 indicates a compressed format.
        buffer.putShort(1)

        // Bytes 22–23: Number of channels = 1 (mono).
        //   Stereo would be 2; we always record mono for the VAD pipeline.
        buffer.putShort(numChannels.toShort())

        // Bytes 24–27: Sample rate = 16 000 Hz.
        //   Must match the rate used by AudioRecord and expected by
        //   faster-whisper on the server.
        buffer.putInt(sampleRate)

        // Bytes 28–31: Byte rate = sampleRate × numChannels × bytesPerSample.
        //   = 16 000 × 1 × 2 = 32 000 bytes/second.
        //   Used by players to calculate buffer sizes and playback duration.
        buffer.putInt(byteRate)

        // Bytes 32–33: Block align = numChannels × bytesPerSample.
        //   = 1 × 2 = 2.  The number of bytes for one complete sample frame
        //   across all channels.
        buffer.putShort(blockAlign.toShort())

        // Bytes 34–35: Bits per sample = 16.
        //   Defines the quantisation depth.  16-bit PCM is the de-facto standard
        //   for speech; it gives 96 dB dynamic range which is more than sufficient.
        buffer.putShort(bitsPerSample.toShort())

        // ── data sub-chunk ────────────────────────────────────────────────────

        // Bytes 36–39: "data" — sub-chunk ID marking the start of PCM samples.
        buffer.put('d'.code.toByte())
        buffer.put('a'.code.toByte())
        buffer.put('t'.code.toByte())
        buffer.put('a'.code.toByte())

        // Bytes 40–43: PCM data size in bytes = floatSamples.size × 2.
        //   This is the number of bytes of raw audio that follows, excluding
        //   any padding.  Players use this to know when the audio stream ends.
        buffer.putInt(pcmDataSize)

        // ── PCM sample data (bytes 44 … end) ──────────────────────────────────
        //
        // Convert each normalised float in [-1.0, 1.0] to a signed 16-bit integer
        // in [-32768, 32767]:
        //
        //   pcm = clamp(sample × 32767, -32768, 32767)
        //
        // We clamp to Short range to guard against the rare case where the
        // microphone or normalisation produces values slightly outside [-1.0, 1.0].
        // The result is written as little-endian Int16, which is the byte order
        // already set on the ByteBuffer.
        for (sample in floatSamples) {
            val pcm = (sample * 32767f)
                .toInt()
                .coerceIn(Short.MIN_VALUE.toInt(), Short.MAX_VALUE.toInt())
                .toShort()
            buffer.putShort(pcm)
        }

        return buffer.array()
    }

    /**
     * Wraps already-encoded 16-bit little-endian mono PCM [pcmBytes] in a
     * standard 44-byte RIFF/WAVE header. Used by [VoiceNoteRecorder], which
     * captures AudioRecord output directly as Int16 PCM bytes.
     *
     * @param pcmBytes    Signed 16-bit little-endian PCM payload (mono).
     * @param sampleRate  Samples per second (16 000 Hz).
     * @return            Complete WAV file as a ByteArray, ready to POST.
     */
    fun pcm16ToWav(pcmBytes: ByteArray, sampleRate: Int = 16_000): ByteArray {
        val numChannels    = 1
        val bitsPerSample  = 16
        val bytesPerSample = bitsPerSample / 8
        val pcmDataSize    = pcmBytes.size
        val riffChunkSize  = 36 + pcmDataSize
        val byteRate       = sampleRate * numChannels * bytesPerSample
        val blockAlign     = numChannels * bytesPerSample

        val buffer = ByteBuffer
            .allocate(44 + pcmDataSize)
            .order(ByteOrder.LITTLE_ENDIAN)

        buffer.put('R'.code.toByte()); buffer.put('I'.code.toByte())
        buffer.put('F'.code.toByte()); buffer.put('F'.code.toByte())
        buffer.putInt(riffChunkSize)
        buffer.put('W'.code.toByte()); buffer.put('A'.code.toByte())
        buffer.put('V'.code.toByte()); buffer.put('E'.code.toByte())

        buffer.put('f'.code.toByte()); buffer.put('m'.code.toByte())
        buffer.put('t'.code.toByte()); buffer.put(' '.code.toByte())
        buffer.putInt(16)
        buffer.putShort(1)
        buffer.putShort(numChannels.toShort())
        buffer.putInt(sampleRate)
        buffer.putInt(byteRate)
        buffer.putShort(blockAlign.toShort())
        buffer.putShort(bitsPerSample.toShort())

        buffer.put('d'.code.toByte()); buffer.put('a'.code.toByte())
        buffer.put('t'.code.toByte()); buffer.put('a'.code.toByte())
        buffer.putInt(pcmDataSize)
        buffer.put(pcmBytes)

        return buffer.array()
    }
}
