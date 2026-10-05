package com.carevoice.app

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.carevoice.app.databinding.ActivityAdminBinding
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.FormBody
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * Admin dispatch console: shows which nurses are free/occupied and the live
 * pending requests, and lets the admin redirect any request to a chosen nurse.
 *
 * Polls GET /admin/dispatch-board every few seconds. Redirect uses the existing
 * POST /alerts/{id}/redirect. All calls carry the admin's JWT.
 */
class AdminActivity : AppCompatActivity() {

    private lateinit var binding: ActivityAdminBinding
    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .build()

    private val handler = Handler(Looper.getMainLooper())
    private lateinit var nurseAdapter: NurseAdapter
    private lateinit var alertAdapter: AlertAdapter

    private var nurses: List<Nurse> = emptyList()

    private val poller = object : Runnable {
        override fun run() {
            fetchBoard()
            handler.postDelayed(this, POLL_MS)
        }
    }

    data class Nurse(val username: String, val fullName: String,
                     val online: Boolean, val busy: Boolean, val activeAlerts: Int)
    data class PendingAlert(val id: Int, val room: String, val patient: String,
                            val priority: String, val intent: String, val summary: String,
                            val routedTo: String?, val fellBack: Boolean)

    private fun serverUrl(): String {
        val prefs = getSharedPreferences(ServerUploader.PREFS_NAME, Context.MODE_PRIVATE)
        return (prefs.getString(ServerUploader.KEY_SERVER_URL, ServerUploader.DEFAULT_SERVER_URL)
            ?: ServerUploader.DEFAULT_SERVER_URL).trimEnd('/')
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityAdminBinding.inflate(layoutInflater)
        setContentView(binding.root)

        val name = UserSession.getFullName(this) ?: "Administrator"
        binding.tvAdminSubtitle.text = "Signed in as $name"

        nurseAdapter = NurseAdapter()
        alertAdapter = AlertAdapter { alert -> showRedirectDialog(alert) }
        binding.rvNurses.layoutManager = LinearLayoutManager(this)
        binding.rvNurses.adapter = nurseAdapter
        binding.rvAlerts.layoutManager = LinearLayoutManager(this)
        binding.rvAlerts.adapter = alertAdapter
    }

    override fun onResume() {
        super.onResume()
        handler.post(poller)
    }

    override fun onPause() {
        super.onPause()
        handler.removeCallbacks(poller)
    }

    // ── Networking ──────────────────────────────────────────────────────────

    private fun authHeader(): String = "Bearer ${UserSession.getToken(this) ?: ""}"

    private fun fetchBoard() {
        lifecycleScope.launch(Dispatchers.IO) {
            try {
                val req = Request.Builder()
                    .url("${serverUrl()}/admin/dispatch-board")
                    .header("Authorization", authHeader())
                    .get().build()
                val body = client.newCall(req).execute().use { r ->
                    if (!r.isSuccessful) {
                        withContext(Dispatchers.Main) {
                            binding.tvStatus.text = "Server error ${r.code}"
                        }
                        return@launch
                    }
                    r.body?.string() ?: return@launch
                }
                val json = JSONObject(body)
                val nurseList = parseNurses(json.optJSONArray("nurses"))
                val alertList = parseAlerts(json.optJSONArray("pending_alerts"))
                withContext(Dispatchers.Main) {
                    nurses = nurseList
                    nurseAdapter.submit(nurseList)
                    alertAdapter.submit(alertList)
                    binding.tvNoAlerts.visibility = if (alertList.isEmpty()) View.VISIBLE else View.GONE
                    binding.tvStatus.text = "Live · updated just now"
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    binding.tvStatus.text = "Offline — retrying…"
                }
            }
        }
    }

    private fun parseNurses(arr: JSONArray?): List<Nurse> {
        val out = mutableListOf<Nurse>()
        if (arr == null) return out
        for (i in 0 until arr.length()) {
            val o = arr.getJSONObject(i)
            out.add(Nurse(
                username = o.optString("username"),
                fullName = o.optString("full_name"),
                online = o.optBoolean("online"),
                busy = o.optBoolean("busy"),
                activeAlerts = o.optInt("active_alerts"),
            ))
        }
        return out
    }

    private fun parseAlerts(arr: JSONArray?): List<PendingAlert> {
        val out = mutableListOf<PendingAlert>()
        if (arr == null) return out
        for (i in 0 until arr.length()) {
            val o = arr.getJSONObject(i)
            out.add(PendingAlert(
                id = o.optInt("id"),
                room = o.optString("room_id"),
                patient = o.optString("patient_name").ifBlank { "Patient" },
                priority = o.optString("priority"),
                intent = o.optString("intent"),
                summary = o.optString("summary"),
                routedTo = o.optString("routed_to").ifBlank { null },
                fellBack = o.optBoolean("fell_back"),
            ))
        }
        return out
    }

    private fun showRedirectDialog(alert: PendingAlert) {
        if (nurses.isEmpty()) {
            Toast.makeText(this, "No nurses loaded yet", Toast.LENGTH_SHORT).show()
            return
        }
        val labels = nurses.map { n ->
            val state = if (n.busy) "occupied" else if (n.online) "free" else "offline"
            "${n.fullName}  ($state)"
        }.toTypedArray()
        AlertDialog.Builder(this)
            .setTitle("Redirect Room ${alert.room} to…")
            .setItems(labels) { _, which ->
                redirect(alert.id, nurses[which].username)
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun redirect(alertId: Int, targetNurse: String) {
        lifecycleScope.launch(Dispatchers.IO) {
            try {
                val payload = JSONObject().put("target_nurse", targetNurse).toString()
                val req = Request.Builder()
                    .url("${serverUrl()}/alerts/$alertId/redirect")
                    .header("Authorization", authHeader())
                    .post(payload.toRequestBody("application/json".toMediaType()))
                    .build()
                val body = client.newCall(req).execute().use { r -> r.body?.string() ?: "" }
                val msg = runCatching { JSONObject(body).optString("message") }.getOrNull()
                withContext(Dispatchers.Main) {
                    Toast.makeText(this@AdminActivity,
                        msg?.ifBlank { "Redirected" } ?: "Redirected", Toast.LENGTH_SHORT).show()
                    fetchBoard()
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    Toast.makeText(this@AdminActivity, "Redirect failed: ${e.message}",
                        Toast.LENGTH_LONG).show()
                }
            }
        }
    }

    // ── Adapters ──────────────────────────────────────────────────────────────

    private inner class NurseAdapter : RecyclerView.Adapter<NurseAdapter.VH>() {
        private var items: List<Nurse> = emptyList()
        fun submit(list: List<Nurse>) { items = list; notifyDataSetChanged() }

        inner class VH(val v: View) : RecyclerView.ViewHolder(v)

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): VH =
            VH(LayoutInflater.from(parent.context).inflate(R.layout.item_admin_nurse, parent, false))

        override fun getItemCount() = items.size

        override fun onBindViewHolder(h: VH, position: Int) {
            val n = items[position]
            h.v.findViewById<android.widget.TextView>(R.id.tvNurseName).text = n.fullName
            h.v.findViewById<android.widget.TextView>(R.id.tvNurseMeta).text =
                "@${n.username} · ${n.activeAlerts} active"
            val pill = h.v.findViewById<android.widget.TextView>(R.id.tvStatusPill)
            val dot  = h.v.findViewById<View>(R.id.dotStatus)
            when {
                !n.online -> { pill.text = "OFFLINE"; tint(pill, dot, 0xFF64748B.toInt()) }
                n.busy    -> { pill.text = "OCCUPIED"; tint(pill, dot, 0xFFDC2626.toInt()) }
                else      -> { pill.text = "FREE"; tint(pill, dot, 0xFF16A34A.toInt()) }
            }
        }

        private fun tint(pill: android.widget.TextView, dot: View, color: Int) {
            pill.backgroundTintList = android.content.res.ColorStateList.valueOf(color)
            dot.backgroundTintList = android.content.res.ColorStateList.valueOf(color)
        }
    }

    private inner class AlertAdapter(
        val onRedirect: (PendingAlert) -> Unit
    ) : RecyclerView.Adapter<AlertAdapter.VH>() {
        private var items: List<PendingAlert> = emptyList()
        fun submit(list: List<PendingAlert>) { items = list; notifyDataSetChanged() }

        inner class VH(val v: View) : RecyclerView.ViewHolder(v)

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): VH =
            VH(LayoutInflater.from(parent.context).inflate(R.layout.item_admin_alert, parent, false))

        override fun getItemCount() = items.size

        override fun onBindViewHolder(h: VH, position: Int) {
            val a = items[position]
            h.v.findViewById<android.widget.TextView>(R.id.tvAlertPriority).text = a.priority
            h.v.findViewById<android.widget.TextView>(R.id.tvAlertRoom).text = "Room ${a.room}"
            val body = if (a.summary.isNotBlank()) "${a.patient} · ${a.summary}"
                       else "${a.patient} · ${a.intent}"
            h.v.findViewById<android.widget.TextView>(R.id.tvAlertBody).text = body
            val routing = when {
                a.fellBack       -> "Broadcast to all (no nurse free)"
                a.routedTo != null -> "Routed to ${a.routedTo}"
                else             -> "Awaiting routing"
            }
            h.v.findViewById<android.widget.TextView>(R.id.tvAlertRouting).text = routing
            h.v.findViewById<View>(R.id.btnRedirect).setOnClickListener { onRedirect(a) }
        }
    }

    companion object {
        private const val POLL_MS = 4000L
    }
}
