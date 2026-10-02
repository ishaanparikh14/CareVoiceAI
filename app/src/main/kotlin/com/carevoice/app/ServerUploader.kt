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
    val intent: String = "",
    val alertId: Int? = null,
    val priority: String = "",
    val language: String = "en"
)

/**
 * ServerUploader — posts a WAV byte array to the on-premises FastAPI server
 * over hospital LAN via HTTP multipart/form-data.
 *
 * All traffic from this class stays on the local network; it never contacts
 * any external service.  The server URL and room ID are user-configurable via
 * Settings and persisted in SharedPreferences so they can be adjusted per ward
 * without rebuilding the app.
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

        // Default LAN placeholder — set the real server IP in Settings.
        const val DEFAULT_SERVER_URL = "http://10.196.51.180:8000"
        //const val DEFAULT_SERVER_URL = "http://10.0.2.2:8000"
        //const val DEFAULT_SERVER_URL = "http://172.17.9.2:8000"
        const val DEFAULT_ROOM_ID    = "4B"
    }

    private val httpClient: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(55,  TimeUnit.SECONDS)
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


    /** Uploads a nurse voice reply for a patient's room/alert.
     *  @return the new voice-note ID on success, or null on failure.
     */
    suspend fun uploadVoiceNote(
        rawBytes: ByteArray,
        roomId: String,
        alertId: Int? = null,
        durationMs: Int = 0,
        language: String = "en"
    ): Int? = withContext(Dispatchers.IO) {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val serverUrl = prefs.getString(KEY_SERVER_URL, DEFAULT_SERVER_URL) ?: DEFAULT_SERVER_URL
        val endpoint = "${serverUrl.trimEnd('/')}/voice-notes"

        // VoiceNoteRecorder returns raw PCM (no WAV header).
        // The backend validates data[:4] == b"RIFF", so we must wrap PCM in a WAV.
        val wavBytes = if (rawBytes.size >= 4 &&
            rawBytes[0] == 'R'.code.toByte() &&
            rawBytes[1] == 'I'.code.toByte() &&
            rawBytes[2] == 'F'.code.toByte() &&
            rawBytes[3] == 'F'.code.toByte()) {
            rawBytes  // already WAV
        } else {
            buildWavFromPcm(rawBytes, sampleRate = 16000, channels = 1, bitsPerSample = 16)
        }

        Log.i(TAG, "Voice note upload started — room=$roomId alertId=$alertId durationMs=$durationMs lang=$language wavBytes=${wavBytes.size}")

        val builder = MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart(
                "audio",
                "voice_reply.wav",
                wavBytes.toRequestBody(MEDIA_TYPE_WAV)
            )
            .addFormDataPart("room_id", roomId)
            .addFormDataPart("duration_ms", durationMs.toString())
            .addFormDataPart("language", if (language == "hi") "hi" else "en")

        if (alertId != null) builder.addFormDataPart("alert_id", alertId.toString())

        val request = Request.Builder()
            .url(endpoint)
            .post(builder.build())
            .apply { UserSession.getToken(context)?.let { addHeader("Authorization", "Bearer $it") } }
            .build()

        return@withContext try {
            httpClient.newCall(request).execute().use { response ->
                val code = response.code
                val body = response.body?.string() ?: ""
                Log.i(TAG, "Voice note upload — HTTP $code | body=$body")
                if (response.isSuccessful) {
                    val noteId = runCatching { org.json.JSONObject(body).optInt("id", 0).takeIf { it > 0 } }.getOrNull()
                    Log.i(TAG, "Voice note uploaded successfully — noteId=$noteId")
                    noteId ?: -1   // -1 = success but no ID in body (shouldn't happen)
                } else {
                    Log.e(TAG, "Voice note upload failed — HTTP $code | body=$body")
                    null
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Voice note upload exception", e)
            null
        }
    }

    /**
     * Wraps raw 16-bit little-endian PCM in a standard WAV container.
     * This is required because [VoiceNoteRecorder] produces bare PCM, but the
     * backend validates for a RIFF header before accepting the upload.
     */
    private fun buildWavFromPcm(
        pcm: ByteArray,
        sampleRate: Int,
        channels: Int,
        bitsPerSample: Int
    ): ByteArray {
        val byteRate = sampleRate * channels * bitsPerSample / 8
        val blockAlign = channels * bitsPerSample / 8
        val dataSize = pcm.size
        val totalSize = 36 + dataSize

        val buf = java.nio.ByteBuffer.allocate(44 + dataSize)
        buf.order(java.nio.ByteOrder.LITTLE_ENDIAN)

        // RIFF chunk
        buf.put('R'.code.toByte()); buf.put('I'.code.toByte())
        buf.put('F'.code.toByte()); buf.put('F'.code.toByte())
        buf.putInt(totalSize)                  // file size - 8
        buf.put('W'.code.toByte()); buf.put('A'.code.toByte())
        buf.put('V'.code.toByte()); buf.put('E'.code.toByte())

        // fmt  sub-chunk
        buf.put('f'.code.toByte()); buf.put('m'.code.toByte())
        buf.put('t'.code.toByte()); buf.put(' '.code.toByte())
        buf.putInt(16)                          // PCM sub-chunk size
        buf.putShort(1)                         // AudioFormat PCM
        buf.putShort(channels.toShort())
        buf.putInt(sampleRate)
        buf.putInt(byteRate)
        buf.putShort(blockAlign.toShort())
        buf.putShort(bitsPerSample.toShort())

        // data sub-chunk
        buf.put('d'.code.toByte()); buf.put('a'.code.toByte())
        buf.put('t'.code.toByte()); buf.put('a'.code.toByte())
        buf.putInt(dataSize)
        buf.put(pcm)

        return buf.array()
    }

    /** Returns the latest voice-note metadata for a room. */
    suspend fun getVoiceNotes(roomId: String): String? = withContext(Dispatchers.IO) {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val serverUrl = prefs.getString(KEY_SERVER_URL, DEFAULT_SERVER_URL) ?: DEFAULT_SERVER_URL
        val url = "${serverUrl.trimEnd('/')}/voice-notes/latest?room_id=${java.net.URLEncoder.encode(roomId, "UTF-8")}"
        val request = Request.Builder()
            .url(url)
            .get()
            .apply { UserSession.getToken(context)?.let { addHeader("Authorization", "Bearer $it") } }
            .build()
        return@withContext try {
            httpClient.newCall(request).execute().use { response ->
                if (response.isSuccessful) response.body?.string() else null
            }
        } catch (e: Exception) {
            Log.e(TAG, "Voice note metadata fetch failed", e)
            null
        }
    }

    fun voiceNoteUrl(noteId: Int): String {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val serverUrl = prefs.getString(KEY_SERVER_URL, DEFAULT_SERVER_URL) ?: DEFAULT_SERVER_URL
        return "${serverUrl.trimEnd('/')}/voice-notes/$noteId/audio"
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
            .addFormDataPart(name = "patient_name", value = UserSession.getFullName(context) ?: "Patient")
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
                    val alertId     = json?.optInt("alert_id", 0)?.takeIf { it > 0 }
                    val priority    = json?.optString("priority", "") ?: ""
                    UploadResult(
                        success = true,
                        shouldAlert = shouldAlert,
                        intent = intent,
                        alertId = alertId,
                        priority = priority,
                        language = json?.optString("language", "en") ?: "en"
                    )
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
}
