package com.carevoice.app

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.UUID

/**
 * Durable queue of patient requests that could not reach the server
 * (no network, server down). Survives app restarts: metadata lives in
 * SharedPreferences, audio in filesDir/pending/. Each entry carries a
 * request_key so a retry the server already processed is not paged twice.
 */
class PendingRequests(private val context: Context) {

    data class Entry(
        val key: String,
        val type: String,          // "audio" | "manual"
        val createdAt: Long,
        val attempts: Int,
    )

    companion object {
        private const val PREF_KEY = "pending_requests_v1"
        const val MAX_ENTRIES = 5
        /** A help request older than this is stale; tell the patient instead. */
        const val MAX_AGE_MS = 20 * 60 * 1000L

        fun newKey(): String = UUID.randomUUID().toString()
    }

    private val prefs get() = context.getSharedPreferences(ServerUploader.PREFS_NAME, Context.MODE_PRIVATE)
    private val dir: File get() = File(context.filesDir, "pending").apply { mkdirs() }

    @Synchronized
    fun all(): List<Entry> {
        val raw = prefs.getString(PREF_KEY, "[]") ?: "[]"
        val arr = runCatching { JSONArray(raw) }.getOrElse { JSONArray() }
        return (0 until arr.length()).mapNotNull { i ->
            runCatching {
                val o = arr.getJSONObject(i)
                Entry(o.getString("key"), o.getString("type"), o.getLong("createdAt"), o.optInt("attempts"))
            }.getOrNull()
        }
    }

    @Synchronized
    private fun save(list: List<Entry>) {
        val arr = JSONArray()
        list.forEach {
            arr.put(JSONObject().put("key", it.key).put("type", it.type)
                .put("createdAt", it.createdAt).put("attempts", it.attempts))
        }
        prefs.edit().putString(PREF_KEY, arr.toString()).apply()
    }

    @Synchronized
    fun addAudio(key: String, wav: ByteArray) {
        File(dir, "$key.wav").writeBytes(wav)
        add(Entry(key, "audio", System.currentTimeMillis(), 1))
    }

    @Synchronized
    fun addManual(key: String) = add(Entry(key, "manual", System.currentTimeMillis(), 1))

    private fun add(e: Entry) {
        val list = all().filter { it.key != e.key }.toMutableList()
        list.add(e)
        while (list.size > MAX_ENTRIES) remove(list.removeAt(0).key, list)
        save(list)
    }

    fun audio(key: String): ByteArray? = File(dir, "$key.wav").takeIf { it.exists() }?.readBytes()

    @Synchronized
    fun bump(key: String) = save(all().map { if (it.key == key) it.copy(attempts = it.attempts + 1) else it })

    @Synchronized
    fun remove(key: String) = remove(key, null)

    private fun remove(key: String, inProgress: MutableList<Entry>?) {
        File(dir, "$key.wav").delete()
        if (inProgress == null) save(all().filter { it.key != key })
    }

    fun isEmpty() = all().isEmpty()
}
