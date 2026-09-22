package com.carevoice.app

import android.content.Context
import android.speech.tts.TextToSpeech
import android.util.Log
import java.util.Locale

/**
 * NurseTts — spoken announcements for Critical alerts on the nurse app.
 *
 * Mirrors the web dashboard behaviour: speaks a SHORT keyword summary
 * (priority + room + intent) in the nurse's chosen language (English / Hindi /
 * Kannada), once per alert. Urgent alerts that the server auto-escalates to
 * Critical are announced too (with an "escalated" prefix).
 *
 * Uses Android's built-in TextToSpeech engine. Language availability depends on
 * the device's installed TTS voices; if a language pack is missing it falls
 * back to the default voice so the announcement is still spoken.
 */
class NurseTts(context: Context) {

    private var tts: TextToSpeech? = null
    private var ready = false
    private val announced = mutableSetOf<Int>()   // alert ids already spoken

    /** Whether announcements are enabled (persisted by the caller). */
    var enabled: Boolean = false

    /** Announcement language: "en" | "hi" | "kn". */
    var lang: String = "en"

    init {
        tts = TextToSpeech(context.applicationContext) { status ->
            ready = status == TextToSpeech.SUCCESS
            if (ready) applyLocale()
            else Log.w(TAG, "TextToSpeech init failed: $status")
        }
    }

    private fun localeFor(l: String): Locale = when (l) {
        "hi" -> Locale("hi", "IN")
        "kn" -> Locale("kn", "IN")
        else -> Locale.US
    }

    private fun applyLocale() {
        val t = tts ?: return
        val res = t.setLanguage(localeFor(lang))
        if (res == TextToSpeech.LANG_MISSING_DATA || res == TextToSpeech.LANG_NOT_SUPPORTED) {
            Log.w(TAG, "TTS language '$lang' not available on this device — using default voice")
            t.setLanguage(Locale.US)
        }
    }

    /** Call when the nurse changes the language selection. */
    fun setLanguage(l: String) {
        lang = l
        if (ready) applyLocale()
    }

    /**
     * Announce an alert if eligible: enabled, Critical, unacknowledged, and not
     * already spoken. `escalated` adds a prefix distinguishing an auto-escalated
     * request from an originally-critical one.
     */
    fun maybeAnnounce(alertId: Int, priority: String, roomId: String, intent: String,
                      escalated: Boolean, acknowledged: Boolean) {
        if (!enabled || acknowledged) return
        if (priority != "Critical") return
        if (!announced.add(alertId)) return   // add() returns false if already present
        speak(sentence(roomId, intent, escalated))
    }

    /** Speak an arbitrary phrase now (used by the "Test voice" action). */
    fun speakTest() {
        speak(sentence("4B", "Emergency", false))
    }

    private fun speak(text: String) {
        val t = tts ?: return
        if (!ready) { Log.w(TAG, "TTS not ready — skipping"); return }
        t.speak(text, TextToSpeech.QUEUE_FLUSH, null, "carevoice-alert")
    }

    /** Build the localized keyword summary. */
    private fun sentence(roomId: String, intent: String, escalated: Boolean): String = when (lang) {
        "hi" -> {
            val i = INTENT_HI[intent] ?: "मदद"
            val head = if (escalated) "बढ़ा हुआ अलर्ट। " else "गंभीर अलर्ट। "
            "${head}कमरा $roomId. मरीज़ को $i की ज़रूरत है। कृपया तुरंत पहुँचें।"
        }
        "kn" -> {
            val i = INTENT_KN[intent] ?: "ಸಹಾಯ"
            val head = if (escalated) "ಉನ್ನತೀಕರಿಸಿದ ಎಚ್ಚರಿಕೆ. " else "ತೀವ್ರ ಎಚ್ಚರಿಕೆ. "
            "${head}ಕೊಠಡಿ $roomId. ರೋಗಿಗೆ $i ಅಗತ್ಯವಿದೆ. ದಯವಿಟ್ಟು ಕೂಡಲೇ ಬನ್ನಿ."
        }
        else -> {
            val i = INTENT_EN[intent] ?: "assistance"
            val head = if (escalated) "Escalated alert. " else "Critical alert. "
            "${head}Room $roomId. Patient needs $i. Please attend immediately."
        }
    }

    fun shutdown() {
        try { tts?.stop(); tts?.shutdown() } catch (_: Exception) {}
        tts = null
        ready = false
    }

    companion object {
        private const val TAG = "NurseTts"

        private val INTENT_EN = mapOf(
            "Emergency" to "an emergency", "Pain" to "severe pain", "Medication" to "medication",
            "Food/Water" to "food or water", "Mobility" to "mobility help", "Hygiene" to "hygiene help",
            "Emotional Support" to "emotional support", "Information" to "information", "Other" to "assistance",
        )
        private val INTENT_HI = mapOf(
            "Emergency" to "आपातकाल", "Pain" to "तेज़ दर्द", "Medication" to "दवाई",
            "Food/Water" to "खाना या पानी", "Mobility" to "चलने में मदद", "Hygiene" to "साफ़-सफ़ाई",
            "Emotional Support" to "सहारा", "Information" to "जानकारी", "Other" to "मदद",
        )
        private val INTENT_KN = mapOf(
            "Emergency" to "ತುರ್ತು ಪರಿಸ್ಥಿತಿ", "Pain" to "ತೀವ್ರ ನೋವು", "Medication" to "ಔಷಧಿ",
            "Food/Water" to "ಆಹಾರ ಅಥವಾ ನೀರು", "Mobility" to "ನಡೆಯಲು ಸಹಾಯ", "Hygiene" to "ಸ್ವಚ್ಛತೆ",
            "Emotional Support" to "ಭಾವನಾತ್ಮಕ ಬೆಂಬಲ", "Information" to "ಮಾಹಿತಿ", "Other" to "ಸಹಾಯ",
        )
    }
}
