package com.carevoice.app

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.util.Log
import com.google.mlkit.common.model.DownloadConditions
import com.google.mlkit.nl.translate.TranslateLanguage
import com.google.mlkit.nl.translate.Translation
import com.google.mlkit.nl.translate.Translator
import com.google.mlkit.nl.translate.TranslatorOptions
import okhttp3.Call
import okhttp3.Callback
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Translation of voice-note transcripts between English, Hindi and German.
 *
 * 1. Server first (POST /translate): an LLM that handles Hinglish, medical
 *    terms and context far better than on-device ML Kit, especially
 *    Hindi → English.
 * 2. On-device ML Kit fallback when the server has no translator (503),
 *    is unreachable, or no [context] was given. The first ML Kit translation in
 *    a direction downloads that language pack once (~30 MB).
 *
 * Callbacks are delivered on the main thread.
 */
class NoteTranslator(private val context: Context? = null) {

    private val TAG = "NoteTranslator"
    private val clients = mutableMapOf<String, Translator>()
    private val main = Handler(Looper.getMainLooper())
    @Volatile private var closed = false

    private val http by lazy {
        OkHttpClient.Builder()
            .connectTimeout(5, TimeUnit.SECONDS)
            .readTimeout(25, TimeUnit.SECONDS)
            .build()
    }

    /** Translate [text] from [from] to [to] ("en" | "hi" | "de"). [onResult] gets null on failure. */
    fun translate(text: String, from: String, to: String, onResult: (String?) -> Unit) {
        if (text.isBlank() || from == to) {
            onResult(text)
            return
        }
        val ctx = context
        val token = ctx?.let { UserSession.getToken(it) }
        if (ctx == null || token == null) {
            translateOnDevice(text, from, to, onResult)
            return
        }
        val base = (ctx.getSharedPreferences(ServerUploader.PREFS_NAME, Context.MODE_PRIVATE)
            .getString(ServerUploader.KEY_SERVER_URL, ServerUploader.DEFAULT_SERVER_URL)
            ?: ServerUploader.DEFAULT_SERVER_URL).trimEnd('/')
        val body = JSONObject().put("text", text.take(2000)).put("source", from).put("target", to)
            .toString().toRequestBody("application/json".toMediaType())
        val req = Request.Builder()
            .url("$base/translate")
            .addHeader("Authorization", "Bearer $token")
            .post(body)
            .build()
        http.newCall(req).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                Log.d(TAG, "Server translation unreachable — using on-device: ${e.message}")
                main.post { if (!closed) translateOnDevice(text, from, to, onResult) }
            }

            override fun onResponse(call: Call, response: Response) {
                val out = response.use { r ->
                    if (!r.isSuccessful) null
                    else runCatching { JSONObject(r.body?.string() ?: "").optString("text") }
                        .getOrNull()?.takeIf { it.isNotBlank() }
                }
                main.post {
                    if (closed) return@post
                    if (out != null) onResult(out)
                    else translateOnDevice(text, from, to, onResult)
                }
            }
        })
    }

    private fun translateOnDevice(text: String, from: String, to: String, onResult: (String?) -> Unit) {
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
        closed = true
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
