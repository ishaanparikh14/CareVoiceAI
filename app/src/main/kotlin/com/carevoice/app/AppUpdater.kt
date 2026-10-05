package com.carevoice.app

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import android.util.Log
import androidx.appcompat.app.AlertDialog
import androidx.core.content.FileProvider
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * In-app auto-update for sideloaded (non-Play-Store) installs.
 *
 * On launch the app asks the server for the latest published build
 * (GET {serverUrl}/app/version). If the manifest's versionCode is newer than
 * this install's BuildConfig.VERSION_CODE, it offers to download the new APK
 * and hands it to the system package installer via a FileProvider URI.
 *
 * Fail-silent by design: a failed check or an offline device never blocks login.
 *
 * NOTE: this only helps devices that ALREADY run a build containing this class.
 * The very first install carrying the updater must be installed manually.
 */
class AppUpdater(private val activity: Activity) {

    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .build()

    /** Check for an update against [serverUrl]; prompt + install if newer. */
    fun checkForUpdate(serverUrl: String) {
        val base = serverUrl.trim().trimEnd('/')
        if (base.isEmpty()) return

        CoroutineScope(Dispatchers.IO).launch {
            try {
                val req = Request.Builder().url("$base/app/version").get().build()
                val body = client.newCall(req).execute().use { resp ->
                    if (!resp.isSuccessful) return@launch
                    resp.body?.string() ?: return@launch
                }
                val json = JSONObject(body)
                val latestCode = json.optInt("versionCode", BuildConfig.VERSION_CODE)
                if (latestCode <= BuildConfig.VERSION_CODE) return@launch   // up to date

                val latestName = json.optString("versionName", "")
                val changelog  = json.optString("changelog", "")
                val apkUrlRaw  = json.optString("apkUrl", "/static/carevoice-latest.apk")
                val apkUrl     = if (apkUrlRaw.startsWith("http")) apkUrlRaw else "$base$apkUrlRaw"
                val mandatory  = json.optBoolean("mandatory", false)

                withContext(Dispatchers.Main) {
                    promptUpdate(latestName, changelog, apkUrl, mandatory)
                }
            } catch (e: Exception) {
                Log.d(TAG, "Update check skipped: ${e.message}")   // fail silent
            }
        }
    }

    private fun promptUpdate(versionName: String, changelog: String, apkUrl: String, mandatory: Boolean) {
        if (activity.isFinishing) return
        val msg = buildString {
            append("A newer version")
            if (versionName.isNotEmpty()) append(" ($versionName)")
            append(" is available.")
            if (changelog.isNotEmpty()) append("\n\n$changelog")
        }
        val dialog = AlertDialog.Builder(activity)
            .setTitle("Update available")
            .setMessage(msg)
            .setCancelable(!mandatory)
            .setPositiveButton("Update now") { _, _ -> ensureCanInstallThenDownload(apkUrl) }
        if (!mandatory) dialog.setNegativeButton("Later", null)
        dialog.show()
    }

    private fun ensureCanInstallThenDownload(apkUrl: String) {
        // Android 8+ requires per-source "install unknown apps" permission.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O &&
            !activity.packageManager.canRequestPackageInstalls()
        ) {
            AlertDialog.Builder(activity)
                .setTitle("Allow installs")
                .setMessage("To update, allow CareVoice to install apps, then tap Update again.")
                .setPositiveButton("Open settings") { _, _ ->
                    val intent = Intent(
                        Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                        Uri.parse("package:${activity.packageName}")
                    )
                    activity.startActivity(intent)
                }
                .setNegativeButton("Cancel", null)
                .show()
            return
        }
        downloadAndInstall(apkUrl)
    }

    private fun downloadAndInstall(apkUrl: String) {
        val progress = AlertDialog.Builder(activity)
            .setTitle("Downloading update…")
            .setMessage("Please wait.")
            .setCancelable(false)
            .create()
        progress.show()

        CoroutineScope(Dispatchers.IO).launch {
            try {
                val dir = File(activity.getExternalFilesDir(null), "updates").apply { mkdirs() }
                // Clean old downloads.
                dir.listFiles()?.forEach { it.delete() }
                val apk = File(dir, "carevoice-update.apk")

                val req = Request.Builder().url(apkUrl).get().build()
                client.newCall(req).execute().use { resp ->
                    if (!resp.isSuccessful) throw Exception("HTTP ${resp.code}")
                    val sink = apk.outputStream()
                    resp.body?.byteStream()?.use { input -> input.copyTo(sink) }
                    sink.close()
                }

                withContext(Dispatchers.Main) {
                    progress.dismiss()
                    launchInstaller(apk)
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    progress.dismiss()
                    AlertDialog.Builder(activity)
                        .setTitle("Update failed")
                        .setMessage("Could not download the update: ${e.message}")
                        .setPositiveButton("OK", null)
                        .show()
                }
            }
        }
    }

    private fun launchInstaller(apk: File) {
        val uri: Uri = FileProvider.getUriForFile(
            activity, "${activity.packageName}.fileprovider", apk
        )
        val intent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(uri, "application/vnd.android.package-archive")
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_GRANT_READ_URI_PERMISSION
        }
        activity.startActivity(intent)
    }

    companion object {
        private const val TAG = "AppUpdater"
    }
}
