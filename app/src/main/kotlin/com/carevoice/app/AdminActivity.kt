package com.carevoice.app

import android.content.Context
import android.content.Intent
import android.content.res.ColorStateList
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.carevoice.app.databinding.ActivityAdminBinding
import com.carevoice.app.databinding.ItemAdminAlertBinding
import com.carevoice.app.databinding.ItemAdminNurseBinding
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.time.Duration
import java.time.Instant
import java.time.LocalTime
import java.time.format.DateTimeFormatter
import java.util.concurrent.TimeUnit

/**
 * Native admin dispatch console. Shows which nurses are free / occupied /
 * offline, the live pending requests, and lets the admin redirect a request to
 * a chosen nurse or mark a nurse occupied/free. Polls /admin/dispatch-board.
 *
 * Simple handling only: alerts are delivered to the patient's assigned nurses
 * (broadcast fallback); the admin can reassign afterwards. No acuity (ESI),
 * competency matching, live-status gating, or escalation.
 */
class AdminActivity : AppCompatActivity() {

    private lateinit var binding: ActivityAdminBinding
    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .build()

    private val handler = Handler(Looper.getMainLooper())
    private val nurseAdapter = NurseAdapter()
    private val alertAdapter = AlertAdapter()

    private var nurses: List<Nurse> = emptyList()
    private var fetching = false
    private var loggedOut = false
    private val redirecting = mutableSetOf<Int>()

    private val poller = object : Runnable {
        override fun run() { fetchBoard(false); handler.postDelayed(this, POLL_MS) }
    }

    data class Nurse(
        val username: String, val fullName: String, val ward: String,
        val online: Boolean, val busy: Boolean, val manualBusy: Boolean,
        val reason: String, val activeAlerts: Int,
    )

    data class PendingAlert(
        val id: Int, val room: String, val patient: String, val priority: String,
        val intent: String, val summary: String, val createdAt: String,
        val routedTo: String?, val fellBack: Boolean,
    )

    // ── Lifecycle ─────────────────────────────────────────────────────────────

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (!UserSession.isLoggedIn(this) || UserSession.getRole(this) != "admin") {
            goToLogin(null); return
        }
        binding = ActivityAdminBinding.inflate(layoutInflater)
        setContentView(binding.root)

        setSupportActionBar(binding.toolbar)
        val name = UserSession.getFullName(this) ?: "Administrator"
        binding.tvGreeting.text = getString(R.string.admin_greeting) + "  " + name

        binding.rvAlerts.layoutManager = LinearLayoutManager(this)
        binding.rvAlerts.adapter = alertAdapter
        binding.rvNurses.layoutManager = LinearLayoutManager(this)
        binding.rvNurses.adapter = nurseAdapter

        binding.tvStatus.text = getString(R.string.admin_refresh) + "…"
    }

    override fun onCreateOptionsMenu(menu: android.view.Menu): Boolean {
        menuInflater.inflate(R.menu.admin_menu, menu); return true
    }

    override fun onOptionsItemSelected(item: android.view.MenuItem): Boolean = when (item.itemId) {
        R.id.action_refresh -> { fetchBoard(true); true }
        R.id.action_logout  -> { confirmLogout(); true }
        else -> super.onOptionsItemSelected(item)
    }

    override fun onResume() {
        super.onResume()
        if (::binding.isInitialized && !loggedOut) {
            fetchBoard(true); handler.postDelayed(poller, POLL_MS)
        }
    }

    override fun onPause() { super.onPause(); handler.removeCallbacks(poller) }

    // ── Session ───────────────────────────────────────────────────────────────

    private fun confirmLogout() {
        AlertDialog.Builder(this)
            .setTitle(R.string.admin_logout)
            .setMessage(R.string.admin_logout_confirm)
            .setPositiveButton(R.string.admin_logout) { _, _ -> goToLogin(null) }
            .setNegativeButton(R.string.settings_cancel, null)
            .show()
    }

    private fun goToLogin(message: Int?) {
        if (loggedOut) return
        loggedOut = true
        handler.removeCallbacks(poller)
        UserSession.logout(this)
        message?.let { Toast.makeText(this, it, Toast.LENGTH_LONG).show() }
        startActivity(Intent(this, LoginActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK
        })
        finish()
    }

    // ── Networking ────────────────────────────────────────────────────────────

    private fun serverUrl(): String {
        val prefs = getSharedPreferences(ServerUploader.PREFS_NAME, Context.MODE_PRIVATE)
        return (prefs.getString(ServerUploader.KEY_SERVER_URL, ServerUploader.DEFAULT_SERVER_URL)
            ?: ServerUploader.DEFAULT_SERVER_URL).trim().trimEnd('/')
    }

    private fun authed(path: String) = Request.Builder()
        .url(serverUrl() + path)
        .header("Authorization", "Bearer ${UserSession.getToken(this) ?: ""}")

    private suspend fun handleAuthFailure(code: Int): Boolean {
        if (code != 401 && code != 403) return false
        withContext(Dispatchers.Main) {
            goToLogin(if (code == 401) R.string.admin_session_expired else R.string.admin_not_admin)
        }
        return true
    }

    private fun fetchBoard(showSpinner: Boolean) {
        if (fetching || loggedOut) return
        fetching = true
        if (showSpinner) binding.progress.visibility = View.VISIBLE
        lifecycleScope.launch(Dispatchers.IO) {
            try {
                client.newCall(authed("/admin/dispatch-board").get().build()).execute().use { resp ->
                    if (handleAuthFailure(resp.code)) return@launch
                    if (!resp.isSuccessful) {
                        withContext(Dispatchers.Main) { binding.tvStatus.text = "Server error (${resp.code})" }
                        return@launch
                    }
                    val json = JSONObject(resp.body?.string() ?: "{}")
                    val nl = parseNurses(json.optJSONArray("nurses"))
                    val al = parseAlerts(json.optJSONArray("pending_alerts"))
                    withContext(Dispatchers.Main) { render(nl, al) }
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    if (!loggedOut) binding.tvStatus.text = getString(R.string.admin_offline_retry)
                }
            } finally {
                fetching = false
                withContext(Dispatchers.Main) { if (!loggedOut) binding.progress.visibility = View.GONE }
            }
        }
    }

    private fun render(nurseList: List<Nurse>, alertList: List<PendingAlert>) {
        if (loggedOut) return
        nurses = nurseList.sortedWith(compareBy({ statusRank(it) }, { it.fullName }))
        nurseAdapter.submit(nurses)
        alertAdapter.submit(alertList)
        binding.tvStatFree.text    = nurses.count { it.online && !it.busy }.toString()
        binding.tvStatBusy.text    = nurses.count { it.online && it.busy }.toString()
        binding.tvStatOffline.text = nurses.count { !it.online }.toString()
        binding.tvStatPending.text = alertList.size.toString()
        binding.tvNoRequests.visibility = if (alertList.isEmpty()) View.VISIBLE else View.GONE
        binding.tvNoNurses.visibility   = if (nurses.isEmpty()) View.VISIBLE else View.GONE
        binding.tvStatus.text = getString(R.string.admin_updated,
            LocalTime.now().format(DateTimeFormatter.ofPattern("HH:mm:ss")))
    }

    private fun statusRank(n: Nurse) = when {
        !n.online -> 2
        n.busy    -> 1
        else      -> 0
    }

    private fun parseNurses(arr: JSONArray?): List<Nurse> {
        if (arr == null) return emptyList()
        return (0 until arr.length()).map { i ->
            val o = arr.getJSONObject(i)
            Nurse(
                username = o.optString("username"),
                fullName = o.optString("full_name").ifBlank { o.optString("username") },
                ward = o.optString("ward").takeUnless { it == "null" } ?: "",
                online = o.optBoolean("online"),
                busy = o.optBoolean("busy"),
                manualBusy = o.optBoolean("manual_busy"),
                reason = o.optString("busy_reason"),
                activeAlerts = o.optInt("active_alerts"),
            )
        }
    }

    private fun parseAlerts(arr: JSONArray?): List<PendingAlert> {
        if (arr == null) return emptyList()
        return (0 until arr.length()).map { i ->
            val o = arr.getJSONObject(i)
            PendingAlert(
                id = o.optInt("id"),
                room = o.optString("room_id"),
                patient = o.optString("patient_name").takeUnless { it.isBlank() || it == "null" } ?: "Patient",
                priority = o.optString("priority"),
                intent = o.optString("intent"),
                summary = o.optString("summary").takeUnless { it == "null" } ?: "",
                createdAt = o.optString("created_at"),
                routedTo = o.optString("routed_to").takeUnless { it.isBlank() || it == "null" },
                fellBack = o.optBoolean("fell_back"),
            )
        }
    }

    // ── Redirect ──────────────────────────────────────────────────────────────

    private fun showRedirectDialog(alert: PendingAlert) {
        val choices = nurses.filter { it.username != alert.routedTo }
        if (choices.isEmpty()) { toast(getString(R.string.admin_no_nurses)); return }
        val labels = choices.map { n -> "${n.fullName}  ·  ${pillLabel(n)}" }.toTypedArray()
        AlertDialog.Builder(this)
            .setTitle(getString(R.string.admin_redirect_title, alert.room))
            .setItems(labels) { _, which -> redirect(alert, choices[which]) }
            .setNegativeButton(R.string.settings_cancel, null)
            .show()
    }

    private fun redirect(alert: PendingAlert, target: Nurse) {
        if (!redirecting.add(alert.id)) return
        alertAdapter.notifyDataSetChanged()
        postJson("/alerts/${alert.id}/redirect", JSONObject().put("target_nurse", target.username)) { ok, msg ->
            redirecting.remove(alert.id)
            toast(msg ?: if (ok) "Redirected" else "Redirect failed")
            fetchBoard(false)
        }
    }

    // ── Nurse actions: mark occupied / free ─────────────────────────────────────

    private fun showNurseActions(n: Nurse) {
        val items = arrayOf(if (n.manualBusy) "Clear 'occupied' flag" else "Mark occupied")
        AlertDialog.Builder(this)
            .setTitle(n.fullName)
            .setItems(items) { _, _ -> setBusy(n, !n.manualBusy) }
            .setNegativeButton(R.string.settings_cancel, null)
            .show()
    }

    private fun setBusy(n: Nurse, busy: Boolean) {
        postJson("/admin/nurses/${n.username}/busy", JSONObject().put("busy", busy)) { ok, _ ->
            toast(if (ok) "${n.fullName} marked ${if (busy) "occupied" else "free"}" else "Could not update")
            fetchBoard(false)
        }
    }

    /** POST JSON with the admin token; callback (ok, message) on main thread. */
    private fun postJson(path: String, body: JSONObject, cb: (Boolean, String?) -> Unit) {
        lifecycleScope.launch(Dispatchers.IO) {
            var ok = false; var message: String? = null
            try {
                val req = authed(path).post(body.toString().toRequestBody("application/json".toMediaType())).build()
                client.newCall(req).execute().use { resp ->
                    if (handleAuthFailure(resp.code)) return@launch
                    val text = resp.body?.string().orEmpty()
                    val json = runCatching { JSONObject(text) }.getOrNull()
                    ok = resp.isSuccessful
                    message = json?.optString("message")?.ifBlank { null }
                        ?: json?.optString("detail")?.ifBlank { null }
                }
            } catch (e: Exception) { message = e.message }
            withContext(Dispatchers.Main) { cb(ok, message) }
        }
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private fun toast(m: String) = Toast.makeText(this, m, Toast.LENGTH_SHORT).show()
    private fun color(id: Int) = ContextCompat.getColor(this, id)
    private fun nameFor(u: String) = nurses.firstOrNull { it.username == u }?.fullName ?: u

    private fun pillLabel(n: Nurse): String = when {
        !n.online -> getString(R.string.admin_pill_offline)
        n.busy    -> getString(R.string.admin_pill_busy)
        else -> getString(R.string.admin_pill_free)
    }

    /** (foreground, background) colors for a nurse's status pill. */
    private fun pillColors(n: Nurse): Pair<Int, Int> = when {
        !n.online -> color(R.color.cv_offline) to color(R.color.cv_offline_bg)
        n.busy    -> color(R.color.cv_busy) to color(R.color.cv_busy_bg)
        else -> color(R.color.cv_free) to color(R.color.cv_free_bg)
    }

    private fun ago(iso: String): String = try {
        val s = Duration.between(Instant.parse(iso), Instant.now()).seconds.coerceAtLeast(0)
        when {
            s < 60 -> "${s}s ago"; s < 3600 -> "${s / 60}m ago"
            s < 86400 -> "${s / 3600}h ago"; else -> "${s / 86400}d ago"
        }
    } catch (_: Exception) { "" }

    // ── Adapters ──────────────────────────────────────────────────────────────

    private inner class NurseAdapter : RecyclerView.Adapter<NurseAdapter.VH>() {
        private var items: List<Nurse> = emptyList()
        fun submit(l: List<Nurse>) { items = l; notifyDataSetChanged() }
        inner class VH(val b: ItemAdminNurseBinding) : RecyclerView.ViewHolder(b.root)
        override fun onCreateViewHolder(p: ViewGroup, v: Int) =
            VH(ItemAdminNurseBinding.inflate(LayoutInflater.from(p.context), p, false))
        override fun getItemCount() = items.size
        override fun onBindViewHolder(h: VH, pos: Int) {
            val n = items[pos]
            h.b.tvNurseName.text = n.fullName
            h.b.tvNurseMeta.text = buildString {
                append("@${n.username}")
                if (n.ward.isNotBlank()) append(" · ${n.ward}")
                append(" · ${n.activeAlerts} active")
                if (n.busy && n.reason.isNotBlank()) append(" · ${n.reason}")
            }
            val (fg, bg) = pillColors(n)
            h.b.tvStatusPill.text = pillLabel(n)
            h.b.tvStatusPill.setTextColor(fg)
            h.b.tvStatusPill.backgroundTintList = ColorStateList.valueOf(bg)
            h.b.dotStatus.backgroundTintList = ColorStateList.valueOf(fg)
            h.b.chipsComp.removeAllViews()
            h.b.chipsComp.visibility = View.GONE
            h.b.cardNurse.setOnClickListener { showNurseActions(n) }
        }
    }

    private inner class AlertAdapter : RecyclerView.Adapter<AlertAdapter.VH>() {
        private var items: List<PendingAlert> = emptyList()
        fun submit(l: List<PendingAlert>) { items = l; notifyDataSetChanged() }
        inner class VH(val b: ItemAdminAlertBinding) : RecyclerView.ViewHolder(b.root)
        override fun onCreateViewHolder(p: ViewGroup, v: Int) =
            VH(ItemAdminAlertBinding.inflate(LayoutInflater.from(p.context), p, false))
        override fun getItemCount() = items.size
        override fun onBindViewHolder(h: VH, pos: Int) {
            val a = items[pos]
            val (fg, bg, strip) = when (a.priority) {
                "Critical" -> Triple(R.color.colorPriorityCritical, R.color.colorPriorityCriticalBg, R.color.colorPriorityCritical)
                "Urgent"   -> Triple(R.color.colorPriorityUrgent, R.color.colorPriorityUrgentBg, R.color.colorPriorityUrgent)
                else       -> Triple(R.color.colorPriorityRoutine, R.color.colorPriorityRoutineBg, R.color.colorPriorityRoutine)
            }
            h.b.tvPriority.text = a.priority.ifBlank { "Routine" }
            h.b.tvPriority.setTextColor(color(fg))
            h.b.tvPriority.backgroundTintList = ColorStateList.valueOf(color(bg))
            h.b.viewPriorityStrip.setBackgroundColor(color(strip))
            h.b.tvRoom.text = "Room ${a.room}"
            h.b.tvTime.text = ago(a.createdAt)
            h.b.tvBody.text = "${a.patient} · ${a.summary.ifBlank { a.intent }}"

            h.b.chipsMeta.removeAllViews()
            h.b.chipsMeta.visibility = View.GONE

            h.b.tvRouting.text = when {
                a.fellBack -> getString(R.string.admin_routed_broadcast)
                a.routedTo != null -> getString(R.string.admin_routed_to, nameFor(a.routedTo))
                else -> getString(R.string.admin_routed_none)
            }
            val busy = a.id in redirecting
            h.b.btnRedirect.isEnabled = !busy
            h.b.btnRedirect.text = if (busy) "Redirecting…" else getString(R.string.admin_redirect)
            h.b.btnRedirect.setOnClickListener { showRedirectDialog(a) }
        }
    }

    companion object { private const val POLL_MS = 4000L }
}
