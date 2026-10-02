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
 * WakeWordDetector
 *
 * Continuously listens for wake words using Android SpeechRecognizer.
 *
 * English wake words:  help · help me · please help · nurse · emergency
 * Hindi wake words:    मदद · मदद करो · नर्स · आपातकाल
 *
 * Once a wake word is detected, onWakeWord() is called.
 * The actual patient request is recorded separately by MainViewModel.
 *
 * ── Language strategy ────────────────────────────────────────────────────────
 *
 * The recognition language is determined ONCE from the app locale at start()
 * time and is NEVER changed because of a recognition error or NO_MATCH.
 *
 *   App locale = English  →  languageTag = "en-IN"  (Indian English accent)
 *   App locale = Hindi    →  languageTag = "hi-IN"
 *
 * Both English and Hindi wake words remain in WAKE_WORDS.  The recognizer
 * language is what controls which acoustic model is used; the wake-word list
 * is just string matching applied to whatever transcription is returned.
 *
 * Why not toggle language on NO_MATCH?
 *   NO_MATCH means the current session produced no usable hypothesis.
 *   It does NOT indicate the user is speaking a different language.
 *   Toggling language on every NO_MATCH creates an oscillating en-IN ↔ hi-IN
 *   loop.  Each language switch requires destroying and recreating the
 *   SpeechRecognizer, which causes ERROR_11 (RECOGNIZER_BUSY) because the ASR
 *   service needs ~1–2 s to release after destroy().  This was the direct cause
 *   of the endless BUSY loop observed in Logcat 2026-09-26.
 *
 * ── Lifecycle ────────────────────────────────────────────────────────────────
 *
 * The recognizer is created ONCE at start() and reused for every session.
 * startListening() is called again on the same instance after each result or
 * recoverable error.  The recognizer is only destroyed on stop(), resume()
 * (the recording session took the mic), or after repeated unrecoverable errors.
 *
 * A boolean flag [sessionActive] tracks whether startListening() has been
 * called and a result/error has not yet arrived.  startListeningSession() is
 * a no-op when sessionActive is true — this is the primary guard against
 * overlapping sessions.
 *
 * ERROR_11 (RECOGNIZER_BUSY) handling:
 *   Do NOT destroy or recreate the recognizer.  The instance is still valid;
 *   the service is temporarily busy.  Retry startListening() after a back-off
 *   delay.  Only after [BUSY_STREAK_RECREATE] consecutive BUSY errors do we
 *   destroy and recreate the recognizer to clear any genuinely stuck state.
 */
class WakeWordDetector(
    private val context: Context,
    private val onWakeWord: () -> Unit
) {

    private val TAG = "WakeWordDetector"

    // -------------------------------------------------------------------------
    // Constants
    // -------------------------------------------------------------------------

    private val WAKE_WORDS = setOf(
        // English
        "help",
        "help me",
        "please help",
        "nurse",
        "emergency",

        // Hindi
        "मदद",
        "मदद करो",
        "नर्स",
        "आपातकाल"
    )

    /**
     * Number of consecutive BUSY errors that trigger a full recognizer
     * destroy-and-recreate cycle.  Below this threshold we simply retry
     * startListening() on the existing instance.
     */
    private val BUSY_STREAK_RECREATE = 4

    // -------------------------------------------------------------------------
    // State
    // -------------------------------------------------------------------------

    private var recognizer: SpeechRecognizer? = null
    private val handler = Handler(Looper.getMainLooper())

    /** True while the detector is running (between start() and stop()). */
    @Volatile private var active = false

    /** True after a wake word fires — prevents any further restart. */
    @Volatile private var triggered = false

    /**
     * True between the call to startListening() and the arrival of
     * onResults() / onError().  startListeningSession() is a no-op while
     * this is true, preventing overlapping recognition sessions.
     */
    @Volatile private var sessionActive = false

    /** Single pending-restart token.  Only one restart is ever in flight. */
    private var pendingRestart: Runnable? = null

    /** Counts consecutive ERROR_RECOGNIZER_BUSY errors. */
    private var busyStreak = 0

    /**
     * The BCP-47 tag used for recognition.  Set once at start() from the app
     * locale.  NEVER changed because of a recognition error.
     */
    private var languageTag = "en-IN"

    // -------------------------------------------------------------------------
    // Intent builder
    // -------------------------------------------------------------------------

    private fun buildIntent(): Intent =
        Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_LANGUAGE, languageTag)
            putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 5)
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            /*
             * Long values are required — SpeechRecognizer silently ignores Int
             * values for these extras, which causes immediate NO_MATCH.
             */
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_MINIMUM_LENGTH_MILLIS, 500L)
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS, 1500L)
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_POSSIBLY_COMPLETE_SILENCE_LENGTH_MILLIS, 1000L)
        }

    // -------------------------------------------------------------------------
    // Availability
    // -------------------------------------------------------------------------

    fun isAvailable(): Boolean = SpeechRecognizer.isRecognitionAvailable(context)

    // -------------------------------------------------------------------------
    // Public API
    // -------------------------------------------------------------------------

    fun start() {
        if (Looper.myLooper() != Looper.getMainLooper()) {
            handler.post { start() }
            return
        }

        if (!isAvailable()) {
            Log.e(TAG, "SpeechRecognizer not available on this device")
            return
        }
        if (active) {
            Log.w(TAG, "start() called while already active — ignored")
            return
        }

        active        = true
        triggered     = false
        sessionActive = false
        busyStreak    = 0

        // Determine recognition language from app locale.
        // This value is fixed for the lifetime of this detector instance.
        val appLocale = androidx.appcompat.app.AppCompatDelegate
            .getApplicationLocales().get(0)
        languageTag = if (appLocale?.language == "hi") "hi-IN" else "en-IN"

        Log.i(TAG, "WakeWordDetector started lang=$languageTag wake_words=$WAKE_WORDS")

        // Create the recognizer once; reuse it for all sessions.
        createRecognizer()

        handler.post { startListeningSession() }
    }

    fun stop() {
        active        = false
        sessionActive = false
        triggered     = false

        cancelPendingRestart()
        handler.removeCallbacksAndMessages(null)

        destroyRecognizer()

        Log.i(TAG, "WakeWordDetector stopped")
    }

    /**
     * Call after the post-wake-word recording session ends, to resume
     * wake-word listening.  The recording holds the mic so we must
     * recreate the recognizer rather than reuse the one from before.
     */
    fun resume() {
        triggered     = false
        sessionActive = false
        busyStreak    = 0

        if (active) {
            // Recreate the recognizer — the mic was held by the recorder.
            createRecognizer()
            scheduleRestart(800L)
        }
    }

    // -------------------------------------------------------------------------
    // Recognizer management
    // -------------------------------------------------------------------------

    private fun createRecognizer() {
        destroyRecognizer()
        try {
            val r = SpeechRecognizer.createSpeechRecognizer(context)
            r.setRecognitionListener(listener)
            recognizer = r
            Log.d(TAG, "WakeWord: recognizer created lang=$languageTag")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to create SpeechRecognizer", e)
            recognizer = null
        }
    }

    private fun destroyRecognizer() {
        sessionActive = false
        val r = recognizer ?: return
        recognizer = null
        try { r.cancel()  } catch (_: Exception) {}
        try { r.destroy() } catch (_: Exception) {}
        Log.d(TAG, "WakeWord: recognizer destroyed")
    }

    // -------------------------------------------------------------------------
    // Session start
    // -------------------------------------------------------------------------

    /**
     * Start one recognition session on the existing recognizer instance.
     *
     * Guards (all must pass):
     *  • active == true            — detector not stopped
     *  • triggered == false        — wake word not yet fired
     *  • sessionActive == false    — no session already in progress
     *
     * If sessionActive is already true this method is a no-op.  This is the
     * primary mechanism that prevents overlapping sessions.
     */
    private fun startListeningSession() {
        if (!active || triggered) return

        if (sessionActive) {
            Log.w(TAG, "WakeWord: startListeningSession() ignored — session already active")
            return
        }

        val r = recognizer ?: run {
            Log.w(TAG, "WakeWord: no recognizer — creating one before starting")
            createRecognizer()
            recognizer ?: run {
                Log.e(TAG, "WakeWord: recognizer creation failed — will retry")
                scheduleRestart(1_500L)
                return
            }
        }

        try {
            sessionActive = true
            r.startListening(buildIntent())
            Log.d(TAG, "WakeWord: startRecognition() lang=$languageTag")
            Log.d(TAG, "SpeechRecognizer listening session started lang=$languageTag")
        } catch (e: Exception) {
            Log.e(TAG, "WakeWord: startListening() threw: ${e.message}")
            sessionActive = false
            // Recreate recognizer and retry after delay.
            createRecognizer()
            scheduleRestart(1_200L)
        }
    }

    // -------------------------------------------------------------------------
    // Restart scheduling
    // -------------------------------------------------------------------------

    /**
     * Schedule a restart, cancelling any previously pending restart first.
     * The restart runnable checks sessionActive before calling
     * startListeningSession() so it is safe to schedule even if a session
     * might still be finishing.
     */
    private fun scheduleRestart(delayMs: Long) {
        cancelPendingRestart()
        val r = Runnable {
            pendingRestart = null
            if (active && !triggered && !sessionActive) {
                startListeningSession()
            }
        }
        pendingRestart = r
        handler.postDelayed(r, delayMs)
        Log.d(TAG, "WakeWord: restart scheduled in ${delayMs}ms lang=$languageTag")
    }

    private fun cancelPendingRestart() {
        pendingRestart?.let { handler.removeCallbacks(it) }
        pendingRestart = null
    }

    // -------------------------------------------------------------------------
    // Recognition listener
    // -------------------------------------------------------------------------

    private val listener = object : RecognitionListener {

        override fun onReadyForSpeech(params: Bundle?) {
            busyStreak = 0
            Log.d(TAG, "SpeechRecognizer ready for speech [lang=$languageTag]")
        }

        override fun onBeginningOfSpeech() {
            Log.d(TAG, "Speech detected — listening...")
        }

        override fun onRmsChanged(rmsdB: Float) {}
        override fun onBufferReceived(buffer: ByteArray?) {}

        override fun onEndOfSpeech() {
            Log.d(TAG, "End of speech")
        }

        // -----------------------------------------------------------------
        // Partial results — check for wake word early
        // -----------------------------------------------------------------

        override fun onPartialResults(partialResults: Bundle) {
            val results = partialResults.getStringArrayList(
                SpeechRecognizer.RESULTS_RECOGNITION) ?: return

            Log.d(TAG, "WakeWord partial results [$languageTag]: $results")
            checkForWakeWord(results, partial = true)
        }

        // -----------------------------------------------------------------
        // Final results
        // -----------------------------------------------------------------

        override fun onResults(results: Bundle) {
            // Session ended cleanly — clear the active flag before anything else.
            sessionActive = false

            val matches = results.getStringArrayList(
                SpeechRecognizer.RESULTS_RECOGNITION) ?: ArrayList()

            Log.d(TAG, "WakeWord results [$languageTag]: $matches")
            checkForWakeWord(matches, partial = false)

            if (!active || triggered) return

            /*
             * Recognition produced a result but it was not a wake word.
             * Restart immediately on the SAME language, SAME recognizer instance.
             *
             * No language toggle.
             * No destroy/recreate.
             * 200 ms delay — the service already released cleanly after onResults.
             */
            scheduleRestart(200L)
        }

        // -----------------------------------------------------------------
        // Errors
        // -----------------------------------------------------------------

        override fun onError(error: Int) {
            // Session ended (with error) — clear the active flag immediately.
            sessionActive = false

            Log.w(TAG, "SpeechRecognizer error: ${errorName(error)} [lang=$languageTag]")

            if (!active || triggered) return

            if (error == SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS) {
                Log.e(TAG, "Microphone permission missing — stopping detector")
                active = false
                return
            }

            when (error) {

                SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> {
                    /*
                     * The ASR service is still processing the previous session.
                     *
                     * Do NOT destroy or recreate the recognizer — doing so was
                     * the root cause of the BUSY loop (destroy creates a gap
                     * during which the old session fires onReadyForSpeech,
                     * resulting in two simultaneous sessions).
                     *
                     * Retry startListening() on the same instance after back-off.
                     * After BUSY_STREAK_RECREATE consecutive failures, recreate
                     * the recognizer to clear genuinely stuck state.
                     */
                    busyStreak++
                    Log.d(TAG, "WakeWord ERROR_11 — recognizer busy; controlled retry (streak=$busyStreak)")

                    if (busyStreak >= BUSY_STREAK_RECREATE) {
                        Log.w(TAG, "WakeWord: BUSY streak=$busyStreak — destroying and recreating recognizer")
                        createRecognizer()   // destroys old internally
                        busyStreak = 0
                        scheduleRestart(2_000L)
                    } else {
                        val backoff = (600L * busyStreak).coerceAtMost(3_000L)
                        scheduleRestart(backoff)
                    }
                }

                SpeechRecognizer.ERROR_NO_MATCH,
                SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> {
                    /*
                     * The recognition session completed without a usable result.
                     *
                     * This does NOT mean the user is speaking a different language.
                     * Language is NEVER toggled because of NO_MATCH.
                     *
                     * Reuse the same recognizer instance.
                     * Restart with the same languageTag after a short delay.
                     */
                    Log.d(TAG, "WakeWord NO_MATCH [$languageTag] — restarting same language")
                    scheduleRestart(500L)
                }

                SpeechRecognizer.ERROR_AUDIO -> {
                    // Hardware-level audio error — recreate to clear state.
                    Log.w(TAG, "WakeWord: audio error — recreating recognizer")
                    createRecognizer()
                    scheduleRestart(1_500L)
                }

                SpeechRecognizer.ERROR_NETWORK,
                SpeechRecognizer.ERROR_NETWORK_TIMEOUT,
                SpeechRecognizer.ERROR_SERVER -> {
                    // Network issue — back off to avoid hammering the service.
                    scheduleRestart(3_000L)
                }

                else -> {
                    // Unknown error — recreate to be safe.
                    createRecognizer()
                    scheduleRestart(1_200L)
                }
            }
        }

        override fun onEvent(eventType: Int, params: Bundle?) {}
    }

    // -------------------------------------------------------------------------
    // Wake-word matching
    // -------------------------------------------------------------------------

    private fun checkForWakeWord(results: List<String>, partial: Boolean) {
        if (triggered || results.isEmpty()) return

        val combined = results.joinToString(" ").lowercase().trim()
        Log.d(TAG, "Checking wake word in: \"$combined\" partial=$partial")

        // Normalize punctuation while preserving Hindi Unicode characters.
        val normalized = combined
            .replace(Regex("[^\\p{L}\\p{N}\\s]"), " ")
            .replace(Regex("\\s+"), " ")
            .trim()

        val matched = WAKE_WORDS.any { wakeWord ->
            normalized.contains(wakeWord.lowercase())
        }

        if (!matched) return

        // ── Wake word detected ────────────────────────────────────────────────
        triggered = true
        cancelPendingRestart()
        handler.removeCallbacksAndMessages(null)

        Log.i(TAG, "WAKE WORD DETECTED: \"$normalized\" partial=$partial lang=$languageTag")

        handler.post {
            try {
                onWakeWord()
            } catch (e: Exception) {
                Log.e(TAG, "Error in onWakeWord callback", e)
            }
        }
    }

    // -------------------------------------------------------------------------
    // Helpers
    // -------------------------------------------------------------------------

    private fun errorName(error: Int): String = when (error) {
        SpeechRecognizer.ERROR_NO_MATCH                  -> "NO_MATCH"
        SpeechRecognizer.ERROR_SPEECH_TIMEOUT            -> "SPEECH_TIMEOUT"
        SpeechRecognizer.ERROR_AUDIO                     -> "AUDIO_ERROR"
        SpeechRecognizer.ERROR_NETWORK                   -> "NETWORK_ERROR"
        SpeechRecognizer.ERROR_NETWORK_TIMEOUT           -> "NETWORK_TIMEOUT"
        SpeechRecognizer.ERROR_RECOGNIZER_BUSY           -> "RECOGNIZER_BUSY (ERROR_11)"
        SpeechRecognizer.ERROR_CLIENT                    -> "CLIENT_ERROR"
        SpeechRecognizer.ERROR_SERVER                    -> "SERVER_ERROR"
        SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS  -> "NO_PERMISSION"
        else                                             -> "ERROR_$error"
    }
}