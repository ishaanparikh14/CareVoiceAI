package com.carevoice.app

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.util.Log

/**
 * WakeWordDetector — continuously listens for the word "help" using Android's
 * on-device SpeechRecognizer (no internet, no API key, works offline).
 *
 * Flow:
 *   1. startListening() kicks off SpeechRecognizer in continuous mode.
 *   2. Every partial/final result is scanned for the trigger word.
 *   3. When "help" is detected, [onWakeWord] is called exactly once.
 *   4. The caller (ViewModel) is responsible for pausing and resuming after
 *      the recording session completes.
 *   5. stop() tears it down cleanly.
 *
 * NOTE: SpeechRecognizer must be created and used on the MAIN thread only.
 */
class WakeWordDetector(
    private val context: Context,
    private val onWakeWord: () -> Unit
) {
    private val TAG = "WakeWordDetector"

    // Wake words — any of these trigger the alert flow
    private val WAKE_WORDS = setOf("help", "help me", "please help", "nurse", "emergency")

    private var recognizer: SpeechRecognizer? = null
    private val handler = Handler(Looper.getMainLooper())
    @Volatile private var active = false
    @Volatile private var triggered = false  // prevent double-firing

    private val recognizerIntent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
        putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
        putExtra(RecognizerIntent.EXTRA_LANGUAGE, "en-US")
        putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 5)
        putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
        putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_MINIMUM_LENGTH_MILLIS, 500L)
        putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS, 1500L)
        putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_POSSIBLY_COMPLETE_SILENCE_LENGTH_MILLIS, 1000L)
    }

    fun isAvailable(): Boolean = SpeechRecognizer.isRecognitionAvailable(context)

    fun start() {
        if (!isAvailable()) {
            Log.w(TAG, "SpeechRecognizer not available on this device")
            return
        }
        active = true
        triggered = false
        handler.post { startRecognition() }
        Log.i(TAG, "Wake word detector started — listening for: $WAKE_WORDS")
    }

    fun stop() {
        active = false
        handler.post {
            recognizer?.cancel()
            recognizer?.destroy()
            recognizer = null
        }
        Log.i(TAG, "Wake word detector stopped")
    }

    /** Call this after the triggered recording session finishes to resume listening. */
    fun resume() {
        triggered = false
        if (active) {
            handler.postDelayed({ startRecognition() }, 500)
        }
    }

    private fun startRecognition() {
        if (!active) return
        recognizer?.cancel()
        recognizer?.destroy()
        recognizer = SpeechRecognizer.createSpeechRecognizer(context)
        recognizer?.setRecognitionListener(listener)
        recognizer?.startListening(recognizerIntent)
        Log.d(TAG, "SpeechRecognizer listening session started")
    }

    private val listener = object : RecognitionListener {

        override fun onPartialResults(partialResults: Bundle) {
            val results = partialResults
                .getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION) ?: return
            checkForWakeWord(results, partial = true)
        }

        override fun onResults(results: Bundle) {
            val matches = results
                .getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION) ?: emptyArrayList()
            checkForWakeWord(matches, partial = false)
            // Restart listening after each recognition session unless triggered
            if (active && !triggered) {
                handler.postDelayed({ startRecognition() }, 100)
            }
        }

        override fun onError(error: Int) {
            val msg = when (error) {
                SpeechRecognizer.ERROR_NO_MATCH         -> "no match"
                SpeechRecognizer.ERROR_SPEECH_TIMEOUT   -> "speech timeout"
                SpeechRecognizer.ERROR_AUDIO            -> "audio error"
                SpeechRecognizer.ERROR_NETWORK          -> "network error"
                SpeechRecognizer.ERROR_NETWORK_TIMEOUT  -> "network timeout"
                SpeechRecognizer.ERROR_RECOGNIZER_BUSY  -> "recognizer busy"
                SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> "no permission"
                else -> "error $error"
            }
            Log.d(TAG, "Recognition error: $msg — restarting")
            // Always restart on error (timeouts are normal between utterances)
            if (active && !triggered) {
                val delay = if (error == SpeechRecognizer.ERROR_RECOGNIZER_BUSY) 800L else 200L
                handler.postDelayed({ startRecognition() }, delay)
            }
        }

        override fun onEndOfSpeech() {}
        override fun onBeginningOfSpeech() {}
        override fun onRmsChanged(rmsdB: Float) {}
        override fun onBufferReceived(buffer: ByteArray?) {}
        override fun onReadyForSpeech(params: Bundle?) {}
        override fun onEvent(eventType: Int, params: Bundle?) {}
    }

    private fun checkForWakeWord(results: List<String>, partial: Boolean) {
        if (triggered) return
        val combined = results.joinToString(" ").lowercase().trim()
        val matched = WAKE_WORDS.any { word -> combined.contains(word) }
        if (matched) {
            triggered = true
            Log.i(TAG, "Wake word detected in: \"$combined\" (partial=$partial)")
            handler.post { onWakeWord() }
        }
    }
}

private fun <T> emptyArrayList(): ArrayList<T> = ArrayList()
