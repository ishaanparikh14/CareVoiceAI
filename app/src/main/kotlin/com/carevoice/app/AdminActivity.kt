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
import android.widget.TextView
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
 * Admin dispatch console.
 *
 *  • Live summary: free / occupied / offline nurses and pending requests.
 *  • Pending requests, each with a Redirect action (POST /alerts/{id}/redirect).
 *  • Nurse list with FREE / OCCUPIED / OFFLINE status; tap a nurse to mark them
 *    occupied or free (POST /admin/nurses/{username}/busy) so the scheduler
 *    skips or uses them.
 *  • Toolbar: Refresh and Log out. An expired or non-admin session is sent back
 *    to the login screen instead of failing silently.
 *
 * Data comes from GET /admin/dispatch-board, polled every few seconds while the
 * screen is visible.
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
        override fun run() {
            fetchBoard(showSpinner = false)
            handler.postDelayed(this, POLL_MS)
        }
    }

    data class Nurse(
        val username: String, val fullName: String, val ward: String,
        val online: Boolean, val busy: Boolean, val manualBusy: Boolean,
        val reason: String, val activeAlerts: Int,
    )

    data class PendingAlert(
        val id: Int, val room: String, val patient: String, val priority: String,
        val intent: String, val summary: String, val createdAt: String,
        val routedTo: String?, val fellBack: Boolean, val rerouteCount: Int,
    )

    // ── Lifecycle ─────────────────────────────────────────────────────────────

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Guard: only a logged-in admin may be here.
        if (!UserSession.isLoggedIn(this) || UserSession.getRole(this) != "admin") {
            goToLogin(null)
            return
        }

        binding = ActivityAdminBinding.inflate(layoutInflater)
        setContentView(binding.root)

        val name = UserSession.getFullName(this) ?: "Administrator"
        binding.toolbar.subtitle = getString(R.string.admin_signed_in_as, name)
        binding.toolbar.inflateMenu(R.menu.admin_menu)
        binding.toolbar.setOnMenuItemClickListener { item ->
            when (item.itemId) {
                R.id.action_refresh -> { fetchBoard(showSpinner = true); true }
                R.id.action_logout  -> { confirmLogout(); true }
                else -> false
            }
        }

        binding.statFree.tvStatLabel.text    = getString(R.string.admin_stat_free)
        binding.statBusy.tvStatLabel.text    = getString(R.string.admin_stat_busy)
        binding.statOffline.tvStatLabel.text = getString(R.string.admin_stat_offline)
        binding.statPending.tvStatLabel.text = getString(R.string.admin_stat_pending)

        binding.rvNurses.layoutManager = LinearLayoutManager(this)
        binding.rvNurses.adapter = nurseAdapter
        binding.rvAlerts.layoutManager = LinearLayoutManager(this)
        binding.rvAlerts.adapter = alertAdapter

        binding.tvStatus.text = getString(R.string.admin_refresh) + "…"
    }

    override fun onResume() {
        super.onResume()
        if (::binding.isInitialized && !loggedOut) {
            fetchBoard(showSpinner = true)
            handler.postDelayed(poller, POLL_MS)
        }
    }

    override fun onPause() {
        super.onPause()
        handler.removeCallbacks(poller)
    }

    // ── Session ───────────────────────────────────────────────────────────────

    private fun confirmLogout() {
        AlertDialog.Builder(this)
            .setTitle(R.string.admin_logout)
            .setMessage(R.string.admin_logout_confirm)
            .setPositiveButton(R.string.admin_logout) { _, _ -> goToLogin(null) }
            .setNegativeButton(R.string.settings_cancel, null)
            .show()
    }

    /** Clear the session and return to the login screen. */
    private fun goToLogin(message: Int?) {
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

    private fun authed(path: String): Request.Builder = Request.Builder()
        .url(serverUrl() + path)
        .header("Authorization", "Bearer ${UserSession.getToken(this) ?: ""}")

    /** Handle 401/403 uniformly. Returns true if the session was ended. */
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
                val req = authed("/admin/dispatch-board").get().build()
                client.newCall(req).execute().use { resp ->
                    if (handleAuthFailure(resp.code)) return@launch
                    if (!resp.isSuccessful) {
                        withContext(Dispatchers.Main) {
                            binding.tvStatus.text = "Server error (${resp.code}). Retrying…"
                        }
                        return@launch
                    }
                    val json = JSONObject(resp.body?.string() ?: "{}")
                    val nurseList = parseNurses(json.optJSONArray("nurses"))
                    val alertList = parseAlerts(json.optJSONArray("pending_alerts"))
                    withContext(Dispatchers.Main) { render(nurseList, alertList) }
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    if (!loggedOut) binding.tvStatus.text = getString(R.string.admin_offline_retry)
                }
            } finally {
                fetching = false
                withContext(Dispatchers.Main) {
                    if (!loggedOut) binding.progress.visibility = View.GONE
                }
            }
        }
    }

    private fun render(nurseList: List<Nurse>, alertList: List<PendingAlert>) {
        if (loggedOut) return
        // Free nurses first, then occupied, then offline; alphabetical within.
        nurses = nurseList.sortedWith(compareBy<Nurse>({ statusRank(it) }, { it.fullName }))
        nurseAdapter.submit(nurses)
        alertAdapter.submit(alertList)

        binding.statFree.tvStatValue.text    = nurses.count { it.online && !it.busy }.toString()
        binding.statBusy.tvStatValue.text    = nurses.count { it.online && it.busy }.toString()
        binding.statOffline.tvStatValue.text = nurses.count { !it.online }.toString()
        binding.statPending.tvStatValue.text = alertList.size.toString()

        binding.tvNoRequests.visibility = if (alertList.isEmpty()) View.VISIBLE else View.GONE
        binding.tvNoNurses.visibility   = if (nurses.isEmpty()) View.VISIBLE else View.GONE
        binding.tvStatus.text = getString(
            R.string.admin_updated, LocalTime.now().format(DateTimeFormatter.ofPattern("HH:mm:ss"))
        )
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
                username     = o.optString("username"),
                fullName     = o.optString("full_name").ifBlank { o.optString("username") },
                ward         = o.optString("ward").takeUnless { it == "null" } ?: "",
                online       = o.optBoolean("online"),
                busy         = o.optBoolean("busy"),
                manualBusy   = o.optBoolean("manual_busy"),
                reason       = o.optString("busy_reason"),
                activeAlerts = o.optInt("active_alerts"),
            )
        }
    }

    private fun parseAlerts(arr: JSONArray?): List<PendingAlert> {
        if (arr == null) return emptyList()
        return (0 until arr.length()).map { i ->
            val o = arr.getJSONObject(i)
            PendingAlert(
                id           = o.optInt("id"),
                room         = o.optString("room_id"),
                patient      = o.optString("patient_name").takeUnless { it.isBlank() || it == "null" } ?: "Patient",
                priority     = o.optString("priority"),
                intent       = o.optString("intent"),
                summary      = o.optString("summary").takeUnless { it == "null" } ?: "",
                createdAt    = o.optString("created_at"),
                routedTo     = o.optString("routed_to").takeUnless { it.isBlank() || it == "null" },
                fellBack     = o.optBoolean("fell_back"),
                rerouteCount = o.optInt("reroute_count"),
            )
        }
    }

    // ── Redirect ──────────────────────────────────────────────────────────────

    private fun showRedirectDialog(alert: PendingAlert) {
        val choices = nurses.filter { it.username != alert.routedTo }
        if (choices.isEmpty()) {
            Toast.makeText(this, R.string.admin_no_nurses, Toast.LENGTH_SHORT).show()
            return
        }
        val labels = choices.map { n ->
            val state = when {
                !n.online -> getString(R.string.admin_pill_offline)
                n.busy    -> getString(R.string.admin_pill_busy)
                else      -> getString(R.string.admin_pill_free)
            }
            "${n.fullName}  ·  $state"
        }.toTypedArray()

        AlertDialog.Builder(this)
            .setTitle(getString(R.string.admin_redirect_title, alert.room))
            .setItems(labels) { _, which -> redirect(alert, choices[which]) }
            .setNegativeButton(R.string.settings_cancel, null)
            .show()
    }

    private fun redirect(alert: PendingAlert, target: Nurse) {
        if (!redirecting.add(alert.id)) return
        alertAdapter.notifyDataSetChanged()

        lifecycleScope.launch(Dispatchers.IO) {
            var message: String
            try {
                val body = JSONObject().put("target_nurse", target.username).toString()
                    .toRequestBody("application/json".toMediaType())
                val req = authed("/alerts/${alert.id}/redirect").post(body).build()
                client.newCall(req).execute().use { resp ->
                    if (handleAuthFailure(resp.code)) return@launch
                    val text = resp.body?.string().orEmpty()
                    val json = runCatching { JSONObject(text) }.getOrNull()
                    message = if (resp.isSuccessful) {
                        json?.optString("message")?.ifBlank { null }
                            ?: "Sent to ${target.fullName}"
                    } else {
                        json?.optString("detail")?.ifBlank { null }
                            ?: "Redirect failed (${resp.code})"
                    }
                }
            } catch (e: Exception) {
                message = "Redirect failed: ${e.message}"
            }
            withContext(Dispatchers.Main) {
                redirecting.remove(alert.id)
                Toast.makeText(this@AdminActivity, message, Toast.LENGTH_LONG).show()
                fetchBoard(showSpinner = false)
            }
        }
    }

    // ── Mark nurse occupied / free ────────────────────────────────────────────

    private fun showNurseActions(nurse: Nurse) {
        val makeBusy = !nurse.manualBusy
        val action = getString(if (makeBusy) R.string.admin_mark_busy else R.string.admin_mark_free)
        AlertDialog.Builder(this)
            .setTitle(nurse.fullName)
            .setMessage(
                buildString {
                    append("@${nurse.username}")
                    if (nurse.ward.isNotBlank()) append(" · ${nurse.ward}")
                    append("\n\nStatus: ")
                    append(when {
                        !nurse.online -> "Offline"
                        nurse.busy    -> "Occupied" + if (nurse.reason.isNotBlank()) " (${nurse.reason})" else ""
                        else          -> "Free"
                    })
                }
            )
            .setPositiveButton(action) { _, _ -> setNurseBusy(nurse, makeBusy) }
            .setNegativeButton(R.string.settings_cancel, null)
            .show()
    }

    private fun setNurseBusy(nurse: Nurse, busy: Boolean) {
        lifecycleScope.launch(Dispatchers.IO) {
            val ok = try {
                val body = JSONObject().put("busy", busy).toString()
                    .toRequestBody("application/json".toMediaType())
                val req = authed("/admin/nurses/${nurse.username}/busy").post(body).build()
                client.newCall(req).execute().use { resp ->
                    if (handleAuthFailure(resp.code)) return@launch
                    resp.isSuccessful
                }
            } catch (e: Exception) { false }
            withContext(Dispatchers.Main) {
                val msg = if (!ok) "Could not update ${nurse.fullName}"
                          else "${nurse.fullName} marked ${if (busy) "occupied" else "free"}"
                Toast.makeText(this@AdminActivity, msg, Toast.LENGTH_SHORT).show()
                fetchBoard(showSpinner = false)
            }
        }
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private fun color(id: Int) = ContextCompat.getColor(this, id)

    private fun ago(iso: String): String = try {
        val secs = Duration.between(Instant.parse(iso), Instant.now()).seconds.coerceAtLeast(0)
        when {
            secs < 60    -> "${secs}s ago"
            secs < 3600  -> "${secs / 60}m ago"
            secs < 86400 -> "${secs / 3600}h ago"
            else         -> "${secs / 86400}d ago"
        }
    } catch (_: Exception) { "" }

    private fun nameFor(username: String): String =
        nurses.firstOrNull { it.username == username }?.fullName ?: username

    // ── Adapters ──────────────────────────────────────────────────────────────

    private inner class NurseAdapter : RecyclerView.Adapter<NurseAdapter.VH>() {
        private var items: List<Nurse> = emptyList()
        fun submit(list: List<Nurse>) { items = list; notifyDataSetChanged() }

        inner class VH(val b: ItemAdminNurseBinding) : RecyclerView.ViewHolder(b.root)

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int) =
            VH(ItemAdminNurseBinding.inflate(LayoutInflater.from(parent.context), parent, false))

        override fun getItemCount() = items.size

        override fun onBindViewHolder(h: VH, position: Int) {
            val n = items[position]
            h.b.tvNurseName.text = n.fullName
            h.b.tvNurseMeta.text = buildString {
                append("@${n.username}")
                if (n.ward.isNotBlank()) append(" · ${n.ward}")
                append(" · ${n.activeAlerts} active")
            }
            val (label, fg, bg) = when {
                !n.online -> Triple(R.string.admin_pill_offline, R.color.cv_offline, R.color.cv_offline_bg)
                n.busy    -> Triple(R.string.admin_pill_busy,    R.color.cv_busy,    R.color.cv_busy_bg)
                else      -> Triple(R.string.admin_pill_free,    R.color.cv_free,    R.color.cv_free_bg)
            }
            h.b.tvStatusPill.setText(label)
            h.b.tvStatusPill.setTextColor(color(fg))
            h.b.tvStatusPill.backgroundTintList = ColorStateList.valueOf(color(bg))
            h.b.dotStatus.backgroundTintList = ColorStateList.valueOf(color(fg))

            h.b.tvNurseReason.visibility = if (n.busy && n.reason.isNotBlank()) View.VISIBLE else View.GONE
            h.b.tvNurseReason.text = n.reason

            h.b.cardNurse.contentDescription = "${n.fullName}, ${getString(label)}"
            h.b.cardNurse.setOnClickListener { showNurseActions(n) }
        }
    }

    private inner class AlertAdapter : RecyclerView.Adapter<AlertAdapter.VH>() {
        private var items: List<PendingAlert> = emptyList()
        fun submit(list: List<PendingAlert>) { items = list; notifyDataSetChanged() }

        inner class VH(val b: ItemAdminAlertBinding) : RecyclerView.ViewHolder(b.root)

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int) =
            VH(ItemAdminAlertBinding.inflate(LayoutInflater.from(parent.context), parent, false))

        override fun getItemCount() = items.size

        override fun onBindViewHolder(h: VH, position: Int) {
            val a = items[position]
            val (fg, bg) = when (a.priority) {
                "Critical" -> R.color.colorPriorityCritical to R.color.colorPriorityCriticalBg
                "Urgent"   -> R.color.colorPriorityUrgent   to R.color.colorPriorityUrgentBg
                else       -> R.color.colorPriorityRoutine  to R.color.colorPriorityRoutineBg
            }
            h.b.tvAlertPriority.text = a.priority.ifBlank { "Routine" }
            h.b.tvAlertPriority.setTextColor(color(fg))
            h.b.tvAlertPriority.backgroundTintList = ColorStateList.valueOf(color(bg))

            h.b.tvAlertRoom.text = "Room ${a.room}"
            h.b.tvAlertTime.text = ago(a.createdAt)
            h.b.tvAlertBody.text = "${a.patient} · ${a.summary.ifBlank { a.intent }}"

            h.b.tvAlertRouting.text = buildString {
                append(when {
                    a.fellBack         -> getString(R.string.admin_routed_broadcast)
                    a.routedTo != null -> getString(R.string.admin_routed_to, nameFor(a.routedTo))
                    else               -> getString(R.string.admin_routed_none)
                })
                if (a.rerouteCount > 0) append(" · rerouted ${a.rerouteCount}×")
            }

            val busy = a.id in redirecting
            h.b.btnRedirect.isEnabled = !busy
            h.b.btnRedirect.setText(if (busy) R.string.admin_redirect_sending else R.string.admin_redirect)
            h.b.btnRedirect.contentDescription = "Redirect Room ${a.room} request"
            h.b.btnRedirect.setOnClickListener { showRedirectDialog(a) }
        }
    }

    companion object {
        private const val POLL_MS = 4000L
    }
}
