package com.carevoice.app

/**
 * Energy-based voice activity detector.
 *
 * Uses RMS thresholding: the ~100× gap between silence (rms ≈ 0.001) and
 * speech (rms ≈ 0.10+) makes a simple threshold as reliable as a neural VAD
 * for this use-case, with zero latency and no model to load.
 */
class SileroVAD(private val threshold: Float = 0.025f) {

    fun isSpeech(audioChunk: FloatArray): Boolean {
        require(audioChunk.size == 512) {
            "SileroVAD expects exactly 512 samples, got ${audioChunk.size}"
        }
        return WavUtils.computeRms(audioChunk) > threshold
    }

    fun resetState() = Unit
    fun close()      = Unit
}
