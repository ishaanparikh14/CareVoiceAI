package com.carevoice.app

import com.google.mlkit.common.model.DownloadConditions
import com.google.mlkit.nl.translate.TranslateLanguage
import com.google.mlkit.nl.translate.Translation
import com.google.mlkit.nl.translate.Translator
import com.google.mlkit.nl.translate.TranslatorOptions

/**
 * On-device translation of voice-note transcripts between English, Hindi and
 * German (Google ML Kit).
 *
 * Translation runs entirely on the phone. The first translation in a direction
 * downloads that language pack once (~30 MB); after that it works offline.
 * Callbacks are delivered on the main thread.
 */
class NoteTranslator {

    private val clients = mutableMapOf<String, Translator>()

    /** Translate [text] from [from] to [to] ("en" | "hi" | "de"). [onResult] gets null on failure. */
    fun translate(text: String, from: String, to: String, onResult: (String?) -> Unit) {
        if (text.isBlank() || from == to) {
            onResult(text)
            return
        }
        val client = clients.getOrPut("$from>$to") {
            Translation.getClient(
                TranslatorOptions.Builder()
                    .setSourceLanguage(code(from))
                    .setTargetLanguage(code(to))
                    .build()
            )
        }
        client.downloadModelIfNeeded(DownloadConditions.Builder().build())
            .addOnSuccessListener {
                client.translate(text)
                    .addOnSuccessListener { onResult(it) }
                    .addOnFailureListener { onResult(null) }
            }
            .addOnFailureListener { onResult(null) }
    }

    fun close() {
        clients.values.forEach { it.close() }
        clients.clear()
    }

    private fun code(lang: String): String = when (lang) {
        "hi" -> TranslateLanguage.HINDI
        "de" -> TranslateLanguage.GERMAN
        else -> TranslateLanguage.ENGLISH
    }

    companion object {
        /**
         * Decision B: translate a note into the viewer's chosen language
         * [viewerLang] ("en" | "hi" | "de"). If the note is already in the
         * viewer's language there is nothing to translate, so we fall back to
         * English (and when the note is already English, to Hindi) so the
         * action still does something useful rather than being a no-op.
         */
        fun targetFor(sourceLang: String, viewerLang: String = "en"): String {
            if (viewerLang != sourceLang) return viewerLang
            return if (sourceLang == "en") "hi" else "en"
        }
    }
}
