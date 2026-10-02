package com.carevoice.app

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import android.speech.tts.Voice
import android.util.Log
import java.util.Locale
import java.util.PriorityQueue
import java.util.concurrent.atomic.AtomicLong

class TtsManager(context: Context) {
    private val appContext = context.applicationContext
    private val mainHandler = Handler(Looper.getMainLooper())

    private var tts: TextToSpeech? = null
    private var ready = false
    private var speaking = false
    private var currentPriority = 0
    private val sequence = AtomicLong(0)

    private data class Item(
        val priority: Int,
        val sequence: Long,
        val text: String,
        val language: String,
        val alertId: Int,
    ) : Comparable<Item> {
        override fun compareTo(other: Item): Int =
            if (priority != other.priority) other.priority.compareTo(priority)
            else sequence.compareTo(other.sequence)
    }

    private val queue = PriorityQueue<Item>()
    private val spokenKeys = mutableSetOf<String>()

    init {
        try {
            val engineRef = arrayOfNulls<TextToSpeech>(1)

            Log.d(TAG, "TTS_CREATE\nInstantiating TextToSpeech...")

            val engine = TextToSpeech(appContext) { status ->
                mainHandler.post {
                    val eng = engineRef[0] ?: run {
                        Log.e(TAG, "TTS init: engineRef[0] is null â€” aborting")
                        return@post
                    }

                    Log.d(TAG, "TTS_INIT\nstatus=$status\nttsInstanceId=${System.identityHashCode(eng)}")

                    if (status == TextToSpeech.SUCCESS) {
                        val initLocale = Locale("en", "IN")
                        val langResult = eng.setLanguage(initLocale)
                        ready = langResult != TextToSpeech.LANG_MISSING_DATA &&
                            langResult != TextToSpeech.LANG_NOT_SUPPORTED

                        if (!ready) {
                            val fallbackResult = eng.setLanguage(Locale.US)
                            ready = fallbackResult != TextToSpeech.LANG_MISSING_DATA &&
                                fallbackResult != TextToSpeech.LANG_NOT_SUPPORTED
                        }
                        
                        Log.d(TAG, "TTS_INIT_READY\ndefaultEngine=${eng.defaultEngine}\nready=$ready")
                        logAvailableVoices(eng)

                        if (ready) drainQueue()
                    }
                }
            }

            engineRef[0] = engine

            engine.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
                override fun onStart(utteranceId: String?) {
                    Log.d(TAG, "TTS_ON_START\nutterance=$utteranceId")
                }

                override fun onDone(utteranceId: String?) {
                    Log.d(TAG, "TTS_ON_DONE\nutterance=$utteranceId")
                    mainHandler.post {
                        speaking = false
                        currentPriority = 0
                        drainQueue()
                    }
                }

                override fun onStop(utteranceId: String?, interrupted: Boolean) {
                    Log.d(TAG, "TTS_ON_STOP\nutterance=$utteranceId\ninterrupted=$interrupted")
                    mainHandler.post {
                        speaking = false
                        currentPriority = 0
                        drainQueue()
                    }
                }

                @Deprecated("Deprecated in Java")
                override fun onError(utteranceId: String?) {
                    Log.d(TAG, "TTS_ON_ERROR\nutterance=$utteranceId")
                    mainHandler.post {
                        speaking = false
                        currentPriority = 0
                        drainQueue()
                    }
                }

                override fun onError(utteranceId: String?, errorCode: Int) {
                    Log.d(TAG, "TTS_ON_ERROR\nutterance=$utteranceId\nerrorCode=$errorCode")
                    mainHandler.post {
                        speaking = false
                        currentPriority = 0
                        drainQueue()
                    }
                }
            })

            tts = engine

        } catch (e: Exception) {
            Log.e(TAG, "TTS initialisation failed", e)
        }
    }

    fun announce(alert: AlertModel) {
        assertMainThread("announce")

        if (alert.attended) return

        val key = "${alert.id}:${alert.priority}:${alert.escalated}"
        if (!spokenKeys.add(key)) {
            Log.d(TAG, "TTS announce: skipping duplicate key=$key")
            return
        }

        val p    = priority(alert)
        val text = buildAutomaticMessage(alert)
        val lang = normaliseLanguage(alert.language)

        Log.d(TAG, "TTS_ANNOUNCE_START\nalertId=${alert.id}\nlanguage=$lang\ntext=$text\ninstanceId=${System.identityHashCode(tts)}")

        if (p == 3) {
            queue.clear()
            speaking = false
            currentPriority = 0
            try { tts?.stop() } catch (_: Exception) {}
            speakNow(alert.id, text, p, lang)
        } else {
            queue.add(Item(p, sequence.incrementAndGet(), text, lang, alert.id))
            drainQueue()
        }
    }

    fun speakSummary(alert: AlertModel) {
        assertMainThread("speakSummary")
        if (alert.attended) return
        val p = priority(alert)
        speakNow(alert.id, buildSummaryMessage(alert), maxOf(2, p), normaliseLanguage(alert.language))
    }

    fun speakVoiceNote(text: String, language: String, noteId: Int) {
        assertMainThread("speakVoiceNote")
        if (text.isBlank()) return

        val normLang  = normaliseLanguage(language)
        val ttsLocale = localeForLanguage(normLang)

        Log.i(TAG,
            "TTS_LANGUAGE_DEBUG:\n" +
            "alertId=$noteId\n" +
            "targetLanguage=$normLang\n" +
            "ttsLocale=$ttsLocale\n" +
            "text=$text"
        )
        Log.d(TAG, "TTS_SPEAK_VOICENOTE\nnoteId=$noteId\nlanguage=$normLang\ntext=$text")

        queue.clear()
        speaking = false
        currentPriority = 0
        try { tts?.stop() } catch (_: Exception) {}
        speakNow(noteId, text, 3, normLang)
    }

    fun reset() {
        assertMainThread("reset")
        queue.clear()
        spokenKeys.clear()
        try { tts?.stop() } catch (_: Exception) {}
        speaking = false
        currentPriority = 0
    }

    fun shutdown() {
        Log.d(TAG, "TTS_SHUTDOWN\ninstanceId=${System.identityHashCode(tts)}")
        try {
            tts?.stop()
            tts?.shutdown()
        } catch (_: Exception) {}
        tts = null
    }

    // â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    // Diagnostic Test
    // â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    fun speakTestEnglish() {
        assertMainThread("speakTestEnglish")
        Log.d(TAG, "speakTestEnglish() CALLED")
        speakNow(-1, "This is an English test.", 1, "en")
    }

    // â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    // Internal helpers
    // â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    private fun priority(alert: AlertModel): Int = when {
        alert.priority == "Critical" || alert.escalated -> 3
        alert.priority == "Urgent"                      -> 2
        else                                            -> 1
    }

    private fun normaliseLanguage(raw: String): String {
        val tag = raw.trim().lowercase()
        return when {
            tag.startsWith("hi") -> "hi"
            tag.startsWith("en") -> "en"
            else -> "en"
        }
    }

    private fun localeForLanguage(language: String): Locale = when (language) {
        "hi" -> Locale("hi", "IN")
        else -> Locale("en", "IN")
    }

    private fun bestVoiceForLanguage(engine: TextToSpeech, language: String): Voice? {
        val voices: Set<Voice> = try {
            engine.voices ?: return null
        } catch (_: Exception) {
            return null
        }

        val targetLang      = if (language == "hi") "hi" else "en"
        val preferredCountry = "IN"
        val secondaryCountry = if (language == "hi") null else "US"

        val matching = voices.filter { it.locale?.language == targetLang }
        if (matching.isEmpty()) return null

        matching.firstOrNull { it.locale?.country == preferredCountry && !it.isNetworkConnectionRequired }
            ?.let { return it }

        matching.firstOrNull { it.locale?.country == preferredCountry }
            ?.let { return it }

        if (secondaryCountry != null) {
            matching.firstOrNull { it.locale?.country == secondaryCountry && !it.isNetworkConnectionRequired }
                ?.let { return it }
            matching.firstOrNull { it.locale?.country == secondaryCountry }
                ?.let { return it }
        }

        matching.firstOrNull { !it.isNetworkConnectionRequired }?.let { return it }

        return matching.firstOrNull()
    }

    private fun buildAutomaticMessage(alert: AlertModel): String {
        val name = alert.patientName.ifBlank { "Patient" }
        val room = alert.roomId
        val requestDetail = when {
            alert.nlpSummary.isNotBlank() -> alert.nlpSummary
            alert.transcript.isNotBlank() && !alert.transcript.startsWith("[") ->
                alert.transcript.take(120)
            else -> null
        }

        return when {
            alert.escalated || alert.priority == "Critical" ->
                if (normaliseLanguage(alert.language) == "hi") {
                    val detail = requestDetail?.let { " à¤®à¤°à¥€à¤œ à¤•à¤¹à¤¤à¥‡ à¤¹à¥ˆà¤‚: $it" } ?: " à¤®à¤°à¥€à¤œ à¤†à¤ªà¤¾à¤¤à¤•à¤¾à¤² à¤®à¥‡à¤‚ à¤¹à¥ˆà¥¤"
                    "à¤—à¤‚à¤­à¥€à¤° à¤†à¤ªà¤¾à¤¤à¤•à¤¾à¤²à¥¤ $name, à¤•à¤®à¤°à¤¾ ${room}à¥¤$detail"
                } else {
                    val detail = requestDetail?.let { " $it" } ?: " The patient is in an emergency."
                    "Critical emergency. $name in Room $room.$detail"
                }
            alert.priority == "Urgent" ->
                if (normaliseLanguage(alert.language) == "hi") {
                    val detail = requestDetail?.let { " à¤®à¤°à¥€à¤œ à¤•à¤¹à¤¤à¥‡ à¤¹à¥ˆà¤‚: $it" } ?: ""
                    "à¤¤à¤¤à¥à¤•à¤¾à¤² à¤…à¤¨à¥à¤°à¥‹à¤§à¥¤ $name, à¤•à¤®à¤°à¤¾ ${room}à¥¤$detail"
                } else {
                    val detail = requestDetail?.let { " $it" } ?: ""
                    "Urgent request. $name in Room $room.$detail"
                }
            else ->
                if (normaliseLanguage(alert.language) == "hi") {
                    val detail = requestDetail?.let { " à¤®à¤°à¥€à¤œ à¤•à¤¹à¤¤à¥‡ à¤¹à¥ˆà¤‚: $it" } ?: ""
                    "à¤¸à¤¾à¤®à¤¾à¤¨à¥à¤¯ à¤…à¤¨à¥à¤°à¥‹à¤§à¥¤ $name, à¤•à¤®à¤°à¤¾ ${room}à¥¤$detail"
                } else {
                    val detail = requestDetail?.let { " $it" } ?: ""
                    "Routine request. $name in Room $room.$detail"
                }
        }
    }

    private fun buildSummaryMessage(alert: AlertModel): String {
        val name = alert.patientName.ifBlank { "Patient" }
        return if (normaliseLanguage(alert.language) == "hi") {
            "$name, à¤•à¤®à¤°à¤¾ ${alert.roomId}. ${alert.nlpSummary}"
        } else {
            "$name, Room ${alert.roomId}. ${alert.nlpSummary}"
        }
    }

    private fun drainQueue() {
        assertMainThread("drainQueue")
        if (!ready || speaking) return
        val next = queue.poll() ?: return
        speakNow(
            alertId  = next.alertId,
            text     = next.text,
            priority = next.priority,
            language = next.language
        )
    }

    private fun speakNow(
        alertId: Int,
        text: String,
        priority: Int,
        language: String
    ) {
        assertMainThread("speakNow")

        val engine = tts ?: run {
            Log.w(TAG, "TTS speakNow: engine not initialised")
            return
        }

        val preferredLocale = localeForLanguage(language)

        var langResult = engine.setLanguage(preferredLocale)

        if (langResult == TextToSpeech.LANG_MISSING_DATA ||
            langResult == TextToSpeech.LANG_NOT_SUPPORTED
        ) {
            when (language) {
                "hi" -> {
                    Log.e(TAG, "TTS: hi-IN locale unavailable â€” skipping alert $alertId")
                    return
                }
                else -> {
                    langResult = engine.setLanguage(Locale.US)
                    if (langResult == TextToSpeech.LANG_MISSING_DATA ||
                        langResult == TextToSpeech.LANG_NOT_SUPPORTED
                    ) {
                        Log.e(TAG, "TTS: en-US also unavailable â€” cannot speak alert $alertId")
                        return
                    }
                }
            }
        }

        val bestVoice = bestVoiceForLanguage(engine, language)
        var voiceResult: String
        if (bestVoice != null) {
            try {
                engine.setVoice(bestVoice)
                voiceResult = "set â†’ ${bestVoice.name} (${bestVoice.locale})"
            } catch (e: Exception) {
                voiceResult = "setVoice failed: ${e.message}"
            }
        } else {
            voiceResult = "no matching voice found (setLanguage only)"
        }

        val activeVoice = try { engine.voice } catch (_: Exception) { null }
        val utteranceId = "carevoice_${alertId}_${System.currentTimeMillis()}"

        Log.d(TAG, "TTS_BEFORE_SPEAK\n" +
            "alertId=$alertId\n" +
            "language=$language\n" +
            "text=$text\n" +
            "requestedLocale=$preferredLocale\n" +
            "activeVoice=${activeVoice?.name}\n" +
            "activeVoiceLocale=${activeVoice?.locale}\n" +
            "ttsInstanceId=${System.identityHashCode(engine)}\n" +
            "speakUtteranceId=$utteranceId\n" +
            "setVoiceResult=$voiceResult")

        if (!ready) {
            Log.w(TAG, "TTS speakNow: engine not ready â€” suppressing speak()")
            return
        }

        speaking = true
        currentPriority = priority

        val speakResult = try {
            engine.speak(
                text,
                TextToSpeech.QUEUE_FLUSH,
                null,
                utteranceId
            )
        } catch (e: Exception) {
            Log.e(TAG, "TTS speak() failed", e)
            -1 // ERROR
        }

        Log.d(TAG, "TTS_SPEAK_CALLED\nalertId=$alertId\nspeakResult=$speakResult\nttsInstanceId=${System.identityHashCode(engine)}")
        
        if (speakResult == TextToSpeech.ERROR) {
            speaking = false
            currentPriority = 0
            drainQueue()
        }
    }

    private fun logAvailableVoices(engine: TextToSpeech) {
        try {
            val voices = engine.voices ?: return
            Log.d(TAG, "TTS_VOICES_TOTAL=${voices.size}")
        } catch (_: Exception) {}
    }

    private fun assertMainThread(caller: String) {
        if (Looper.myLooper() != Looper.getMainLooper()) {
            Log.e(TAG, "TTS.$caller() called off main thread! Thread=${Thread.currentThread().name}")
        }
    }

    companion object {
        private const val TAG = "TtsManager"
    }
}
