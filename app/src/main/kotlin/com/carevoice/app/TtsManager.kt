package com.carevoice.app

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.speech.tts.TextToSpeech
import android.util.Log
import java.util.Locale

/**
 * TtsManager — speaks the transcript of a received voice note aloud in the
 * note's own language (English or Hindi).
 *
 * This is intentionally scoped to voice-note playback only; alert announcements
 * are handled separately by [NurseTts]. Keeping them apart avoids two engines
 * fighting over the same TextToSpeech instance.
 */
class TtsManager(context: Context) {

    private val appContext = context.applicationContext
    private val mainHandler = Handler(Looper.getMainLooper())
    private var tts: TextToSpeech? = null
    private var ready = false

    init {
        tts = TextToSpeech(appContext) { status ->
            mainHandler.post {
                ready = status == TextToSpeech.SUCCESS
                if (ready) {
                    tts?.setLanguage(Locale("en", "IN"))
                } else {
                    Log.w(TAG, "TextToSpeech init failed: $status")
                }
            }
        }
    }

    /** Speak [text] in [language] ("en" | "hi"). Interrupts anything in progress. */
    fun speakVoiceNote(text: String, language: String, noteId: Int) {
        if (text.isBlank()) return
        val engine = tts ?: return
        if (!ready) {
            Log.w(TAG, "TTS not ready — skipping voice note $noteId")
            return
        }
        val lang = normaliseLanguage(language)
        val locale = localeForLanguage(lang)
        val res = engine.setLanguage(locale)
        if (res == TextToSpeech.LANG_MISSING_DATA || res == TextToSpeech.LANG_NOT_SUPPORTED) {
            if (lang == "hi") {
                Log.e(TAG, "TTS: hi-IN locale unavailable — cannot speak note $noteId")
                return
            }
            engine.setLanguage(Locale.US)
        }
        engine.stop()
        engine.speak(text, TextToSpeech.QUEUE_FLUSH, null, "voicenote_$noteId")
        Log.d(TAG, "Speaking voice note $noteId (lang=$lang)")
    }

    fun shutdown() {
        try {
            tts?.stop()
            tts?.shutdown()
        } catch (_: Exception) {}
        tts = null
        ready = false
    }

    private fun normaliseLanguage(raw: String): String {
        val tag = raw.trim().lowercase()
        return when {
            tag.startsWith("hi") -> "hi"
            else -> "en"
        }
    }

    private fun localeForLanguage(language: String): Locale = when (language) {
        "hi" -> Locale("hi", "IN")
        else -> Locale("en", "IN")
    }

    companion object {
        private const val TAG = "TtsManager"
    }
}
