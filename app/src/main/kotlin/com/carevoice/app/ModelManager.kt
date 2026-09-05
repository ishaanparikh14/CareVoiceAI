package com.carevoice.app

import android.content.Context
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.io.FileOutputStream
import java.util.concurrent.TimeUnit

/**
 * ModelManager — ensures silero_vad.onnx is present on disk before the app
 * begins recording.
 *
 * The only internet contact in the app — every other call goes to the
 * on-premises FastAPI server over the hospital LAN.
 */
object ModelManager {

    private const val TAG       = "ModelManager"
    private const val MODEL_DIR = "models"

    val SILERO_VAD = ModelDef(
        "silero_vad.onnx",
        "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx"
    )

    data class ModelDef(val fileName: String, val url: String)

    private val downloadClient: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(30, TimeUnit.SECONDS)
            .readTimeout(120, TimeUnit.SECONDS)
            .build()
    }

    suspend fun ensureModelsReady(
        context: Context,
        onProgress: (Int) -> Unit
    ): Map<String, String> = withContext(Dispatchers.IO) {
        val paths = mutableMapOf<String, String>()
        val path  = downloadModel(context, SILERO_VAD, onProgress)
        paths[SILERO_VAD.fileName] = path
        onProgress(100)
        paths
    }

    private suspend fun downloadModel(
        context: Context,
        modelDef: ModelDef,
        onProgress: (Int) -> Unit
    ): String = withContext(Dispatchers.IO) {
        val modelsDir = File(context.filesDir, MODEL_DIR)
        val modelFile = File(modelsDir, modelDef.fileName)

        if (modelFile.exists() && modelFile.length() > 0) {
            onProgress(100)
            return@withContext modelFile.absolutePath
        }

        if (!modelsDir.exists()) modelsDir.mkdirs()
        Log.i(TAG, "Downloading ${modelDef.fileName}")

        val response = downloadClient.newCall(
            Request.Builder().url(modelDef.url).build()
        ).execute()

        if (!response.isSuccessful) throw Exception("Download failed: HTTP ${response.code}")

        val body          = response.body ?: throw Exception("Empty response body")
        val contentLength = response.header("Content-Length")?.toLongOrNull() ?: -1L
        val tempFile      = File(modelsDir, "${modelDef.fileName}.tmp")

        try {
            body.byteStream().use { input ->
                FileOutputStream(tempFile).use { output ->
                    val buffer = ByteArray(8 * 1024)
                    var bytesRead: Int
                    var totalRead  = 0L
                    var lastProgress = -1

                    while (input.read(buffer).also { bytesRead = it } != -1) {
                        output.write(buffer, 0, bytesRead)
                        totalRead += bytesRead
                        if (contentLength > 0) {
                            val p = ((totalRead * 100L) / contentLength).toInt().coerceIn(0, 99)
                            if (p != lastProgress) { lastProgress = p; onProgress(p) }
                        }
                    }
                    output.flush()
                }
            }
            if (!tempFile.renameTo(modelFile)) throw Exception("Rename failed")
            onProgress(100)
            modelFile.absolutePath
        } catch (e: Exception) {
            tempFile.delete()
            throw e
        }
    }
}
