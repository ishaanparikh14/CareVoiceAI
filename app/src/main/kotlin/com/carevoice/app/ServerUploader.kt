package com.carevoice.app

import android.content.Context
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * Result of an audio upload attempt.
 * @param success      HTTP 200/201 received
 * @param shouldAlert  Server decided this audio warrants notifying nurses
 * @param intent       Classified intent string (e.g. "Pain", "Other")
 */
data class UploadResult(
    val success: Boolean,
    val shouldAlert: Boolean = true,
    val intent: String = ""
)

/** A room assigned to the logged-in nurse (from GET /auth/patients). */
data class PatientRoom(val room: String, val patientName: String)

/**
 * A single voice note returned by the /voice-notes endpoints.
 * Mirrors VoiceNoteResponse in routers/voice_notes.py.
 */
data class VoiceNote(
    val id: Int,
    val roomId: String,
    val alertId: Int?,
    val senderRole: String,
    val senderName: String,
    val createdAt: String,
    val durationMs: Int,
    val language: String,
    val audioUrl: String,
    val originalText: String?
)

/**
 * ServerUploader — posts a WAV byte array to the FastAPI server via HTTP
 * multipart/form-data.
 *
 * The server URL and room ID are user-configurable via Settings and persisted
 * in SharedPreferences, so they can point at the hosted endpoint or a per-ward
 * LAN address without rebuilding the app.
 *
 * @param context  Application or Activity context; used only to access
 *                 SharedPreferences (no UI work performed here).
 */
class ServerUploader(private val context: Context) {

    private val TAG = "ServerUploader"

    // ── SharedPreferences keys and defaults ───────────────────────────────────

    companion object {
        const val PREFS_NAME    = "carevoice_prefs"
        const val KEY_SERVER_URL = "server_url"
        const val KEY_ROOM_ID    = "room_id"

        // Default server URL. Can be overridden in Settings (e.g. a hospital LAN IP).
        const val DEFAULT_SERVER_URL = "https://3-110-163-212.sslip.io"
        const val DEFAULT_ROOM_ID    = "4B"
    }

    private val httpClient: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(90,  TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .build()

    private val MEDIA_TYPE_WAV = "audio/wav".toMediaType()

    // ── Public API ────────────────────────────────────────────────────────────

    /**
     * Sends an instant manual "Call Nurse" alert — no audio, no recording.
     * Creates a Critical/Emergency alert on the server immediately.
     * Returns true on success.
     */
    suspend fun sendManualAlert(): Boolean = withContext(Dispatchers.IO) {
        val prefs       = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val serverUrl   = prefs.getString(KEY_SERVER_URL, DEFAULT_SERVER_URL) ?: DEFAULT_SERVER_URL
        val roomId      = prefs.getString(KEY_ROOM_ID, DEFAULT_ROOM_ID) ?: DEFAULT_ROOM_ID
        val patientName = UserSession.getFullName(context) ?: "Patient"

        val url = "${serverUrl.trimEnd('/')}/alerts/manual?room_id=${
            java.net.URLEncoder.encode(roomId, "UTF-8")
        }&patient_name=${
            java.net.URLEncoder.encode(patientName, "UTF-8")
        }"

        Log.d(TAG, "Sending manual alert to $url")

        val request = Request.Builder()
            .url(url)
            .post(ByteArray(0).toRequestBody(null))
            .apply {
                UserSession.getToken(context)?.let { addHeader("Authorization", "Bearer $it") }
            }
            .build()

        return@withContext try {
            httpClient.newCall(request).execute().use { response ->
                val success = response.code in 200..201
                Log.i(TAG, "Manual alert — HTTP ${response.code}")
                success
            }
        } catch (e: Exception) {
            Log.e(TAG, "Manual alert failed", e)
            false
        }
    }

    /**
     * Uploads [wavBytes] to {server_url}/audio/ingest as a multipart POST.
     *
     * @param wavBytes  Complete WAV file produced by [WavUtils.toWavByteArray].
     * @return          [UploadResult] with success flag, shouldAlert, and intent.
     */
    suspend fun uploadAudio(wavBytes: ByteArray): UploadResult = withContext(Dispatchers.IO) {

        val prefs     = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val serverUrl = prefs.getString(KEY_SERVER_URL, DEFAULT_SERVER_URL) ?: DEFAULT_SERVER_URL
        val roomId    = prefs.getString(KEY_ROOM_ID,    DEFAULT_ROOM_ID)    ?: DEFAULT_ROOM_ID
        val endpoint  = "$serverUrl/audio/ingest"

        Log.d(TAG, "Uploading ${wavBytes.size} bytes to $endpoint (room: $roomId)")

        val requestBody = MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart(
                name     = "audio",
                filename = "recording.wav",
                body     = wavBytes.toRequestBody(MEDIA_TYPE_WAV)
            )
            .addFormDataPart(name = "room_id", value = roomId)
            .build()

        val request = Request.Builder()
            .url(endpoint)
            .post(requestBody)
            .apply {
                UserSession.getToken(context)?.let { addHeader("Authorization", "Bearer $it") }
            }
            .build()

        return@withContext try {
            httpClient.newCall(request).execute().use { response ->
                val code     = response.code
                val bodyText = response.body?.string() ?: ""

                if (code == 200 || code == 201) {
                    Log.i(TAG, "Upload success — HTTP $code")
                    val json        = runCatching { JSONObject(bodyText) }.getOrNull()
                    val shouldAlert = json?.optBoolean("should_alert", true) ?: true
                    val intent      = json?.optString("intent", "") ?: ""
                    UploadResult(success = true, shouldAlert = shouldAlert, intent = intent)
                } else {
                    Log.e(TAG, "Upload failed — HTTP $code | body: $bodyText")
                    UploadResult(success = false)
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Upload exception — $endpoint", e)
            UploadResult(success = false)
        }
    }

    // ── Voice notes ─────────────────────────────────────────────────────────

    private fun serverUrl(): String {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        return (prefs.getString(KEY_SERVER_URL, DEFAULT_SERVER_URL) ?: DEFAULT_SERVER_URL)
            .trimEnd('/')
    }

    private fun roomId(): String {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        return prefs.getString(KEY_ROOM_ID, DEFAULT_ROOM_ID) ?: DEFAULT_ROOM_ID
    }

    /**
     * Uploads a voice note WAV to POST /voice-notes.
     *
     * @param wavBytes    Complete WAV produced by [VoiceNoteRecorder].
     * @param roomId      Target room (defaults to this device's configured room).
     * @param language    "en" | "hi" — the sender's language.
     * @param durationMs  Clip duration in milliseconds.
     * @param alertId     Optional alert this note is attached to.
     * @return            true on HTTP 200/201.
     */
    suspend fun uploadVoiceNote(
        wavBytes: ByteArray,
        roomId: String = roomId(),
        language: String = "en",
        durationMs: Int = 0,
        alertId: Int? = null,
    ): Boolean = withContext(Dispatchers.IO) {
        val endpoint = "${serverUrl()}/voice-notes"
        val builder = MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart("audio", "voice_note.wav", wavBytes.toRequestBody(MEDIA_TYPE_WAV))
            .addFormDataPart("room_id", roomId)
            .addFormDataPart("language", language)
            .addFormDataPart("duration_ms", durationMs.toString())
        if (alertId != null) builder.addFormDataPart("alert_id", alertId.toString())

        val request = Request.Builder()
            .url(endpoint)
            .post(builder.build())
            .apply { UserSession.getToken(context)?.let { addHeader("Authorization", "Bearer $it") } }
            .build()

        return@withContext try {
            httpClient.newCall(request).execute().use { response ->
                val ok = response.code in 200..201
                Log.i(TAG, "Voice note upload — HTTP ${response.code}")
                ok
            }
        } catch (e: Exception) {
            Log.e(TAG, "Voice note upload failed", e)
            false
        }
    }

    /** Fetches the latest voice notes for [roomId] via GET /voice-notes/latest. */
    suspend fun listVoiceNotes(roomId: String = roomId()): List<VoiceNote> =
        fetchNotes("${serverUrl()}/voice-notes/latest?room_id=${
            java.net.URLEncoder.encode(roomId, "UTF-8")
        }")

    /**
     * Nurse inbox: voice notes across all of the calling nurse's assigned rooms
     * (GET /voice-notes/inbox). Returns empty for non-nurses or on error.
     */
    suspend fun inboxVoiceNotes(): List<VoiceNote> =
        fetchNotes("${serverUrl()}/voice-notes/inbox")

    private suspend fun fetchNotes(endpoint: String): List<VoiceNote> = withContext(Dispatchers.IO) {
        val request = Request.Builder()
            .url(endpoint)
            .get()
            .apply { UserSession.getToken(context)?.let { addHeader("Authorization", "Bearer $it") } }
            .build()

        return@withContext try {
            httpClient.newCall(request).execute().use { response ->
                if (response.code !in 200..201) return@use emptyList()
                val body = response.body?.string() ?: return@use emptyList()
                val arr = org.json.JSONArray(body)
                (0 until arr.length()).map { i ->
                    val o = arr.getJSONObject(i)
                    VoiceNote(
                        id           = o.getInt("id"),
                        roomId       = o.optString("room_id"),
                        alertId      = if (o.isNull("alert_id")) null else o.optInt("alert_id"),
                        senderRole   = o.optString("sender_role"),
                        senderName   = o.optString("sender_name"),
                        createdAt    = o.optString("created_at"),
                        durationMs   = o.optInt("duration_ms"),
                        language     = o.optString("language", "en"),
                        audioUrl     = o.optString("audio_url"),
                        originalText = if (o.isNull("original_text")) null else o.optString("original_text"),
                    )
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Fetch voice notes failed ($endpoint)", e)
            emptyList()
        }
    }

    /** Rooms assigned to the logged-in nurse (GET /auth/patients), sorted by room. */
    suspend fun listMyPatients(): List<PatientRoom> = withContext(Dispatchers.IO) {
        val request = Request.Builder()
            .url("${serverUrl()}/auth/patients")
            .get()
            .apply { UserSession.getToken(context)?.let { addHeader("Authorization", "Bearer $it") } }
            .build()
        return@withContext try {
            httpClient.newCall(request).execute().use { response ->
                if (response.code !in 200..201) return@use emptyList()
                val arr = org.json.JSONArray(response.body?.string() ?: return@use emptyList())
                (0 until arr.length())
                    .map { i -> arr.getJSONObject(i) }
                    .map { o -> PatientRoom(o.optString("room_number").trim(), o.optString("full_name").trim()) }
                    .filter { it.room.isNotEmpty() }
                    .distinctBy { it.room }
                    .sortedBy { it.room }
            }
        } catch (e: Exception) {
            Log.e(TAG, "List assigned patients failed", e)
            emptyList()
        }
    }

    /** Downloads the raw WAV bytes for a voice note (GET /voice-notes/{id}/audio). */
    suspend fun fetchVoiceNoteAudio(noteId: Int): ByteArray? = withContext(Dispatchers.IO) {
        val endpoint = "${serverUrl()}/voice-notes/$noteId/audio"
        val request = Request.Builder()
            .url(endpoint)
            .get()
            .apply { UserSession.getToken(context)?.let { addHeader("Authorization", "Bearer $it") } }
            .build()
        return@withContext try {
            httpClient.newCall(request).execute().use { response ->
                if (response.code !in 200..201) null else response.body?.bytes()
            }
        } catch (e: Exception) {
            Log.e(TAG, "Fetch voice note audio failed", e)
            null
        }
    }
}
