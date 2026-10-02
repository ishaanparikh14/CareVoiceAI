package com.carevoice.app

import android.content.Context
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import java.io.FileOutputStream

/**
 * ModelManager — ensures silero_vad.onnx is present on disk before the app
 * begins recording.
 *
 * LAN-ONLY ARCHITECTURE: the model ships INSIDE the APK as an asset
 * (app/src/main/assets/models/silero_vad.onnx) and is copied to app-private
 * storage on first run. There is no network call anywhere in this class.
 * Every other network call in the app goes to the on-premises FastAPI server
 * over the hospital LAN only — this keeps that guarantee airtight, since a
 * hospital's outbound firewall will typically block github.com anyway.
 */
object ModelManager {

    private const val TAG       = "ModelManager"
    private const val MODEL_DIR = "models"
    private const val ASSET_SUBDIR = "models"

    val SILERO_VAD = ModelDef("silero_vad.onnx")

    data class ModelDef(val fileName: String)

    suspend fun ensureModelsReady(
        context: Context,
        onProgress: (Int) -> Unit
    ): Map<String, String> = withContext(Dispatchers.IO) {
        val paths = mutableMapOf<String, String>()
        val path  = copyModelFromAssets(context, SILERO_VAD, onProgress)
        paths[SILERO_VAD.fileName] = path
        onProgress(100)
        paths
    }

    private fun copyModelFromAssets(
        context: Context,
        modelDef: ModelDef,
        onProgress: (Int) -> Unit
    ): String {
        val modelsDir = File(context.filesDir, MODEL_DIR)
        val modelFile = File(modelsDir, modelDef.fileName)

        // Already copied on a previous run — nothing to do.
        if (modelFile.exists() && modelFile.length() > 0) {
            onProgress(100)
            return modelFile.absolutePath
        }

        if (!modelsDir.exists()) modelsDir.mkdirs()

        val assetPath = "$ASSET_SUBDIR/${modelDef.fileName}"
        Log.i(TAG, "Copying $assetPath from app assets (no network)")

        val tempFile = File(modelsDir, "${modelDef.fileName}.tmp")

        return try {
            context.assets.open(assetPath).use { input ->
                val totalSize = input.available().toLong() // -1 if compressed; handled below
                FileOutputStream(tempFile).use { output ->
                    val buffer = ByteArray(64 * 1024)
                    var bytesRead: Int
                    var totalRead = 0L
                    var lastProgress = -1

                    while (input.read(buffer).also { bytesRead = it } != -1) {
                        output.write(buffer, 0, bytesRead)
                        totalRead += bytesRead
                        if (totalSize > 0) {
                            val p = ((totalRead * 100L) / totalSize).toInt().coerceIn(0, 99)
                            if (p != lastProgress) { lastProgress = p; onProgress(p) }
                        }
                    }
                    output.flush()
                }
            }
            if (!tempFile.renameTo(modelFile)) throw Exception("Rename failed: ${tempFile.path} -> ${modelFile.path}")
            onProgress(100)
            Log.i(TAG, "Model ready at ${modelFile.absolutePath}")
            modelFile.absolutePath
        } catch (e: Exception) {
            tempFile.delete()
            throw Exception(
                "Failed to load bundled model asset '$assetPath'. " +
                    "Make sure silero_vad.onnx is placed at app/src/main/assets/models/silero_vad.onnx.",
                e
            )
        }
        }
}