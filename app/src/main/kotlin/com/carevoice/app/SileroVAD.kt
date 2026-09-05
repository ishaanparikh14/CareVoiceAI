package com.carevoice.app

/**
 * Energy-based voice activity detector.
 *
 * Uses RMS thresholding instead of the Silero ONNX model — the 100× gap
 * between silence (rms ≈ 0.001) and speech (rms ≈ 0.10+) makes a simple
 * threshold as reliable as a neural VAD for this use-case, with zero latency.
 *
 * The [modelPath] parameter is kept for API compatibility but is not used.
 */
class SileroVAD(
    @Suppress("UNUSED_PARAMETER") modelPath: String,
    private val threshold: Float = 0.025f
) {
    fun isSpeech(audioChunk: FloatArray): Boolean {
        require(audioChunk.size == 512) {
            "SileroVAD expects exactly 512 samples, got ${audioChunk.size}"
        }
        return WavUtils.computeRms(audioChunk) > threshold
    }

    fun resetState() = Unit
    fun close()      = Unit
}
