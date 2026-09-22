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

        // Default: the public cloud (Modal) server so testers work from anywhere
        // out of the box. Can be overridden in Settings (e.g. a hospital LAN IP).
        const val DEFAULT_SERVER_URL = "https://ishaansnehalp-cs24--carevoice-fastapi-app.modal.run"
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
}
