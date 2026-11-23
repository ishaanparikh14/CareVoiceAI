package com.carevoice.app

import android.content.Context
import android.content.Intent
import android.media.AudioManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.util.Log

/**
 * WakeWordDetector — listens for "help" / "nurse" / "madad" etc. with Android's
 * SpeechRecognizer.
 *
 * v2.9 changes
 *  - Silent: the recognizer plays a start/stop beep on every listening session,
 *    which restarts every few seconds. While the detector is active the beep
 *    streams are muted (and always restored on stop, even after a crash —
 *    see [restoreIfLeftMuted]).
 *  - Sensitivity (Low / Normal / High, in Settings): Normal adds common
 *    mis-hearings and Hindi words; High also accepts near-matches.
 *  - [onHeard] reports what was heard when no wake word matched, so the screen
 *    can tell the patient "heard '…' — say 'Help'" instead of silently ignoring it.
 *
 * SpeechRecognizer must be created and used on the MAIN thread only.
 */
class WakeWordDetector(
    private val context: Context,
    private val onHeard: (String) -> Unit = {},
    private val onWakeWord: () -> Unit,
) {
    private val TAG = "WakeWordDetector"

    companion object {
        const val KEY_SENSITIVITY = "wake_sensitivity"   // "low" | "normal" | "high"
        private const val KEY_MUTED_BY_US = "wake_streams_muted"

        private val BASE = listOf("help", "help me", "please help", "nurse", "emergency")
        private val NORMAL_EXTRA = listOf(
            "helps", "helped", "helping", "help please", "nurse please", "sister", "sister please",
            "madad", "bachao", "bachaao", "नर्स", "मदद", "बचाओ", "सिस्टर", "हेल्प",
        )
        private val FUZZY_TARGETS = listOf("help", "nurse", "madad", "bachao", "emergency")

        /** Pure matcher (unit-testable). */
        fun matches(text: String, sensitivity: String): Boolean {
            val t = text.lowercase().trim()
            if (t.isEmpty()) return false
            val words = when (sensitivity) {
                "low" -> BASE
                else -> BASE + NORMAL_EXTRA
            }
            val tokens = t.split(Regex("[^\\p{L}\\p{M}]+")).filter { it.isNotEmpty() }
            if (words.any { w -> if (w.contains(' ')) t.contains(w) else tokens.contains(w) }) return true
            if (sensitivity == "high") {
                return tokens.any { tok ->
                    tok.length >= 4 && FUZZY_TARGETS.any { target -> editDistance(tok, target) <= 1 }
                }
            }
            return false
        }

        private fun editDistance(a: String, b: String): Int {
            val dp = IntArray(b.length + 1) { it }
            for (i in 1..a.length) {
                var prev = dp[0]
                dp[0] = i
                for (j in 1..b.length) {
                    val tmp = dp[j]
                    dp[j] = minOf(dp[j] + 1, dp[j - 1] + 1, prev + if (a[i - 1] == b[j - 1]) 0 else 1)
                    prev = tmp
                }
            }
            return dp[b.length]
        }

        /** Unmute streams if the app was killed while the detector had them muted. */
        fun restoreIfLeftMuted(context: Context) {
            val prefs = context.getSharedPreferences(ServerUploader.PREFS_NAME, Context.MODE_PRIVATE)
            if (prefs.getBoolean(KEY_MUTED_BY_US, false)) {
                setBeepStreamsMuted(context, false)
                prefs.edit().putBoolean(KEY_MUTED_BY_US, false).apply()
            }
        }

        private val BEEP_STREAMS = intArrayOf(
            AudioManager.STREAM_MUSIC, AudioManager.STREAM_NOTIFICATION, AudioManager.STREAM_SYSTEM,
        )

        private fun setBeepStreamsMuted(context: Context, mute: Boolean) {
            val am = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
            val dir = if (mute) AudioManager.ADJUST_MUTE else AudioManager.ADJUST_UNMUTE
            for (s in BEEP_STREAMS) {
                try { am.adjustStreamVolume(s, dir, 0) } catch (_: Exception) { /* DND may refuse */ }
            }
        }
    }

    private var recognizer: SpeechRecognizer? = null
    private val handler = Handler(Looper.getMainLooper())
    @Volatile private var active = false
    @Volatile private var triggered = false  // prevent double-firing
    private var muted = false
    private var consecutiveErrors = 0

    private val sensitivity: String
        get() = context.getSharedPreferences(ServerUploader.PREFS_NAME, Context.MODE_PRIVATE)
            .getString(KEY_SENSITIVITY, "normal") ?: "normal"

    private fun buildIntent(): Intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
        putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
        // Indian English recognises "help"/"nurse"/"sister" with local accents better.
        putExtra(RecognizerIntent.EXTRA_LANGUAGE, "en-IN")
        putExtra("android.speech.extra.EXTRA_ADDITIONAL_LANGUAGES", arrayOf("hi-IN"))
        putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 10)
        putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
        putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, context.packageName)
        // Short utterances ("help") count; keep sessions long so restarts are rare.
        putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_MINIMUM_LENGTH_MILLIS, 300L)
        putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS, 2500L)
        putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_POSSIBLY_COMPLETE_SILENCE_LENGTH_MILLIS, 2000L)
    }

    fun isAvailable(): Boolean = SpeechRecognizer.isRecognitionAvailable(context)

    fun start() {
        if (!isAvailable()) {
            Log.w(TAG, "SpeechRecognizer not available on this device")
            return
        }
        active = true
        triggered = false
        consecutiveErrors = 0
        handler.post {
            mute(true)
            startRecognition()
        }
        Log.i(TAG, "Wake word detector started (sensitivity=$sensitivity)")
    }

    fun stop() {
        active = false
        handler.removeCallbacksAndMessages(null)
        handler.post {
            recognizer?.cancel()
            recognizer?.destroy()
            recognizer = null
            // Give the recognizer's own stop beep time to be swallowed, then restore.
            handler.postDelayed({ if (!active) mute(false) }, 400)
        }
        Log.i(TAG, "Wake word detector stopped")
    }

    private fun mute(on: Boolean) {
        if (muted == on) return
        muted = on
        setBeepStreamsMuted(context, on)
        context.getSharedPreferences(ServerUploader.PREFS_NAME, Context.MODE_PRIVATE)
            .edit().putBoolean(KEY_MUTED_BY_US, on).apply()
    }

    private fun startRecognition() {
        if (!active) return
        recognizer?.cancel()
        recognizer?.destroy()
        recognizer = SpeechRecognizer.createSpeechRecognizer(context)
        recognizer?.setRecognitionListener(listener)
        recognizer?.startListening(buildIntent())
    }

    private fun restart(delayMs: Long) {
        if (active && !triggered) handler.postDelayed({ startRecognition() }, delayMs)
    }

    private val listener = object : RecognitionListener {

        override fun onPartialResults(partialResults: Bundle) {
            val results = partialResults.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION) ?: return
            checkForWakeWord(results, partial = true)
        }

        override fun onResults(results: Bundle) {
            consecutiveErrors = 0
            val matches = results.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION) ?: ArrayList()
            checkForWakeWord(matches, partial = false)
            if (!triggered) {
                val heard = matches.firstOrNull()?.trim().orEmpty()
                if (heard.isNotEmpty()) handler.post { onHeard(heard) }
            }
            restart(150)
        }

        override fun onError(error: Int) {
            // Timeouts / no-match are normal between utterances. Back off on
            // repeated real errors so we don't spin the recognizer (and the mic).
            val benign = error == SpeechRecognizer.ERROR_NO_MATCH ||
                error == SpeechRecognizer.ERROR_SPEECH_TIMEOUT
            consecutiveErrors = if (benign) 0 else consecutiveErrors + 1
            val delay = when {
                error == SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> 800L
                benign -> 150L
                else -> minOf(500L * (1 shl minOf(consecutiveErrors, 4)), 8_000L)
            }
            Log.d(TAG, "Recognition error $error — restarting in ${delay}ms")
            restart(delay)
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
        val sens = sensitivity
        val hit = results.firstOrNull { matches(it, sens) }
        if (hit != null) {
            triggered = true
            Log.i(TAG, "Wake word detected in: \"$hit\" (partial=$partial, sensitivity=$sens)")
            handler.post { onWakeWord() }
        }
    }
}
