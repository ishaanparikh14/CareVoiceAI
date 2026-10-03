package com.carevoice.app

import android.content.Context
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import java.io.ByteArrayOutputStream
import kotlin.math.min

/** Records a short 16 kHz mono PCM voice note and returns it as a WAV byte array. */
class VoiceNoteRecorder(private val context: Context) {

    companion object {
        private const val TAG = "VoiceNoteRecorder"
        private const val SAMPLE_RATE = 16_000
        private const val MAX_SECONDS = 30
    }

    private var recordJob: Job? = null
    @Volatile private var recording = false
    private var startedAt = 0L
    private var onFinished: ((ByteArray, Int) -> Unit)? = null

    fun isRecording(): Boolean = recording

    fun setOnFinished(callback: (ByteArray, Int) -> Unit) {
        onFinished = callback
    }

    fun start(scope: CoroutineScope, onStarted: () -> Unit = {}) {
        if (recording) return
        recording = true
        startedAt = System.currentTimeMillis()
        onStarted()
        recordJob = scope.launch(Dispatchers.IO) {
            val minBuffer = AudioRecord.getMinBufferSize(
                SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT
            )
            val bufferSize = maxOf(minBuffer * 2, 4096)
            val recorder = try {
                AudioRecord(
                    MediaRecorder.AudioSource.VOICE_RECOGNITION,
                    SAMPLE_RATE,
                    AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT,
                    bufferSize
                )
            } catch (e: Exception) {
                Log.e(TAG, "AudioRecord construction failed", e)
                null
            }
            if (recorder == null || recorder.state != AudioRecord.STATE_INITIALIZED) {
                recorder?.release()
                recording = false
                Log.e(TAG, "AudioRecord failed to initialise")
                // Report an empty clip so the UI leaves the "Recording…" state.
                scope.launch(Dispatchers.Main) { onFinished?.invoke(ByteArray(0), 0) }
                return@launch
            }
            val pcm = ByteArrayOutputStream(SAMPLE_RATE * 2 * 5)
            val shortBuffer = ShortArray(1024)
            try {
                recorder.startRecording()
                while (recording && System.currentTimeMillis() - startedAt < MAX_SECONDS * 1000L) {
                    val read = recorder.read(shortBuffer, 0, shortBuffer.size)
                    if (read <= 0) continue
                    for (i in 0 until read) {
                        val v = shortBuffer[i].toInt()
                        pcm.write(v and 0xFF)
                        pcm.write((v shr 8) and 0xFF)
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "Voice note recording failed", e)
            } finally {
                try { recorder.stop() } catch (_: Exception) {}
                recorder.release()
                val duration = min(
                    System.currentTimeMillis() - startedAt,
                    MAX_SECONDS * 1000L
                ).toInt()
                recording = false
                // Wrap raw PCM into a WAV container so the server sees a RIFF file.
                val wav = WavUtils.pcm16ToWav(pcm.toByteArray(), SAMPLE_RATE)
                scope.launch(Dispatchers.Main) {
                    onFinished?.invoke(wav, duration)
                }
            }
        }
    }

    fun stop() {
        recording = false
        recordJob?.cancel()
        recordJob = null
    }
}
