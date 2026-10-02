package com.carevoice.app

import android.content.Context
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.PowerManager
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

class AudioRecorder(
    private val context: Context,
    private val sileroVAD: SileroVAD,
    private val scope: CoroutineScope,
    private val onTrigger: () -> Unit,
    private val onSpeechDetected: suspend (FloatArray) -> Unit,
    private val onRmsUpdate: (Float) -> Unit = {}
) {
    private val TAG = "AudioRecorder"

    private val SAMPLE_RATE   = 16_000
    private val CHUNK_SAMPLES = 512

    private var recordingJob: Job? = null
    @Volatile private var keepRunning = false
    private var wakeLock: PowerManager.WakeLock? = null

    enum class State { IDLE, RECORDING_COMMAND }

    fun start() {
        stop() // ensure clean state
        keepRunning = true
        sileroVAD.resetState()

        // Keep CPU alive so the coroutine isn't throttled when screen turns off.
        val pm = context.getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "CareVoice:mic")
            .also { it.acquire(10 * 60 * 1000L) }

        recordingJob = scope.launch(Dispatchers.IO) {
            runRecordingLoop()
        }
    }

    fun stop() {
        keepRunning = false
        recordingJob?.cancel()
        recordingJob = null
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
    }

    private fun runRecordingLoop() {
        // VOICE_RECOGNITION: bypasses noise-suppression AGC and Android's
        // privacy mic-silencing that can zero-out the MIC source on some devices.
        val minBuf = AudioRecord.getMinBufferSize(
            SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
        )
        val recorder = AudioRecord(
            MediaRecorder.AudioSource.VOICE_RECOGNITION,
            SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            minBuf * 4
        )

        if (recorder.state != AudioRecord.STATE_INITIALIZED) {
            Log.e(TAG, "AudioRecord failed to initialise — cannot record")
            return
        }

        recorder.startRecording()
        Log.i(TAG, "Recording started. minBuf=$minBuf keepRunning=$keepRunning")

        val shortBuf           = ShortArray(CHUNK_SAMPLES)
        val commandAccumulator = mutableListOf<FloatArray>()
        var state              = State.IDLE
        var silenceCounter     = 0
        var speechCounter      = 0
        val maxCommandChunks   = (SAMPLE_RATE * 10) / CHUNK_SAMPLES  // 10s hard cap
        val silenceThreshold   = 0.018f

        try {
            while (keepRunning) {
                val read = recorder.read(shortBuf, 0, CHUNK_SAMPLES)

                if (!keepRunning) break  // stop() was called mid-read

                if (read != CHUNK_SAMPLES) {
                    if (read < 0) Log.e(TAG, "read() error code $read — stopping")
                    if (read < 0) break
                    continue  // partial read, skip
                }

                val floatChunk = FloatArray(CHUNK_SAMPLES) { i -> shortBuf[i] / 32768f }
                val rms = WavUtils.computeRms(floatChunk)
                onRmsUpdate(rms)

                if (rms < 0.00005f) {
                    Log.w(
                        TAG,
                        "MIC TEST: RMS=$rms — microphone appears silent"
                    )
                } else {
                    Log.d(
                        TAG,
                        "MIC TEST: RMS=$rms"
                    )
                }
                val isSpeech = sileroVAD.isSpeech(floatChunk)

                when (state) {
                    State.IDLE -> {
                        if (isSpeech) {
                            speechCounter++
                            if (speechCounter >= 3) {
                                state = State.RECORDING_COMMAND
                                onTrigger()
                                speechCounter = 0
                                Log.i(TAG, "Speech onset — recording utterance")
                            }
                        } else {
                            speechCounter = 0
                        }
                    }

                    State.RECORDING_COMMAND -> {
                        commandAccumulator.add(floatChunk)
                        val isSilentNow = rms < silenceThreshold
                        if (isSilentNow) {
                            silenceCounter++
                            if (silenceCounter >= 50) { // ~1.6s trailing silence
                                val utterance = commandAccumulator
                                    .flatMap { it.toList() }
                                    .toFloatArray()
                                commandAccumulator.clear()
                                silenceCounter = 0
                                state = State.IDLE
                                Log.i(TAG, "Utterance complete (silence): ${utterance.size} samples")
                                scope.launch(Dispatchers.IO) { onSpeechDetected(utterance) }
                            }
                        } else {
                            silenceCounter = 0
                        }
                        // Hard cap: send after 10s regardless
                        if (commandAccumulator.size >= maxCommandChunks) {
                            val utterance = commandAccumulator.flatMap { it.toList() }.toFloatArray()
                            commandAccumulator.clear()
                            silenceCounter = 0
                            state = State.IDLE
                            Log.i(TAG, "Utterance complete (max duration): ${utterance.size} samples")
                            scope.launch(Dispatchers.IO) { onSpeechDetected(utterance) }
                        }
                    }
                }
            }
        } finally {
            try {
                recorder.stop()
                recorder.release()
            } catch (_: Exception) {}
            Log.i(TAG, "Recording loop ended")
        }
    }
}
