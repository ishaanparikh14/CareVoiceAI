package com.carevoice.app

import com.google.mlkit.common.model.DownloadConditions
import com.google.mlkit.nl.translate.TranslateLanguage
import com.google.mlkit.nl.translate.Translation
import com.google.mlkit.nl.translate.Translator
import com.google.mlkit.nl.translate.TranslatorOptions

/**
 * On-device English ↔ Hindi translation of voice-note transcripts (Google ML Kit).
 *
 * Translation runs entirely on the phone. The first translation in a direction
 * downloads that language pack once (~30 MB); after that it works offline.
 * Callbacks are delivered on the main thread.
 */
class NoteTranslator {

    private val clients = mutableMapOf<String, Translator>()

    /** Translate [text] from [from] to [to] ("en" | "hi"). [onResult] gets null on failure. */
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

    private fun code(lang: String): String =
        if (lang == "hi") TranslateLanguage.HINDI else TranslateLanguage.ENGLISH

    companion object {
        /** The other supported language: notes in Hindi translate to English and vice versa. */
        fun targetFor(sourceLang: String): String = if (sourceLang == "hi") "en" else "hi"
    }
}
