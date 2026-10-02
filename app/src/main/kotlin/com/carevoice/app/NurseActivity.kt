package com.carevoice.app

import android.content.Context
import android.media.MediaPlayer
import android.content.Intent
import android.os.Bundle
import android.view.MenuItem
import android.view.View
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.GravityCompat
import androidx.appcompat.app.AppCompatDelegate
import androidx.core.os.LocaleListCompat
import androidx.lifecycle.lifecycleScope
import androidx.recyclerview.widget.LinearLayoutManager
import com.carevoice.app.databinding.ActivityNurseBinding
import com.google.android.material.navigation.NavigationView
import com.google.android.material.snackbar.Snackbar
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * NurseActivity — real-time nurse dashboard with left navigation drawer.
 *
 * Drawer provides: profile card (name, ward), Settings, Logout.
 * Dashboard: RecyclerView alert cards, All/Pending filter, WS live indicator,
 * HTTP poll fallback every 15 s.
 */
class NurseActivity : AppCompatActivity(), NavigationView.OnNavigationItemSelectedListener {

    private lateinit var binding: ActivityNurseBinding
    private lateinit var adapter: AlertAdapter
    private lateinit var ttsManager: TtsManager

    // ── HTTP + WS clients ─────────────────────────────────────────────────────

    private val http = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()

    private val wsHttp = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.SECONDS)
        .build()

    // ── State ─────────────────────────────────────────────────────────────────

    private var webSocket: WebSocket? = null
    private var pollJob: Job? = null
    private var wsReconnectJob: Job? = null

    private val allAlerts = mutableListOf<AlertModel>()
    private var showPendingOnly = false

    // Rooms assigned to this nurse — alerts are filtered to these.
    // Empty set = not yet loaded → show all (fail-open so nothing is missed).
    private val myRooms = mutableSetOf<String>()

    // ── Accessors ─────────────────────────────────────────────────────────────

    private val serverUrl: String
        get() {
            val prefs = getSharedPreferences(ServerUploader.PREFS_NAME, Context.MODE_PRIVATE)
            return (prefs.getString(ServerUploader.KEY_SERVER_URL, ServerUploader.DEFAULT_SERVER_URL)
                ?: ServerUploader.DEFAULT_SERVER_URL).trimEnd('/')
        }

    private val token: String? get() = UserSession.getToken(this)
    private val nurseName: String get() = UserSession.getFullName(this) ?: "Nurse"

    private var pendingVoiceReply: AlertModel? = null
    private var voiceNoteRecorder: VoiceNoteRecorder? = null
    private var patientVoicePlayer: MediaPlayer? = null

    private val requestVoiceNotePermission =
        registerForActivityResult(androidx.activity.result.contract.ActivityResultContracts.RequestPermission()) { granted ->
            val alert = pendingVoiceReply
            if (granted && alert != null) showVoiceReplyDialog(alert)
            else if (!granted) Toast.makeText(
                this,
                getString(R.string.nurse_voice_permission),
                Toast.LENGTH_LONG
            ).show()
        }

    // ── Lifecycle ─────────────────────────────────────────────────────────────

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityNurseBinding.inflate(layoutInflater)
        setContentView(binding.root)

        setupToolbar()
        setupDrawer()
        populateDrawerHeader()
        ttsManager = TtsManager(this)
        setupRecyclerView()
        setupFilterBar()

        // Sync nurse's saved language preference to the server on startup
        // so the server always has up-to-date preferred_language for
        // patient → nurse voice note translation, even before the nurse
        // explicitly changes language in this session.
        syncNurseLanguageOnStart()

        // Load this nurse's assigned rooms first, then alerts (filtered to them).
        loadMyRoomsThenAlerts()
        connectWebSocket()
        startPollLoop()
    }

    override fun onDestroy() {
        super.onDestroy()
        pollJob?.cancel()
        wsReconnectJob?.cancel()
        webSocket?.cancel()
        ttsManager.shutdown()
        try { patientVoicePlayer?.release() } catch (_: Exception) {}
        patientVoicePlayer = null
    }

    override fun onBackPressed() {
        if (binding.drawerLayout.isDrawerOpen(GravityCompat.START)) {
            binding.drawerLayout.closeDrawer(GravityCompat.START)
        } else {
            super.onBackPressed()
        }
    }

    // ── Toolbar ───────────────────────────────────────────────────────────────

    private fun setupToolbar() {
        setSupportActionBar(binding.toolbar)
        supportActionBar?.apply {
            setDisplayShowTitleEnabled(false)
            setDisplayHomeAsUpEnabled(true)
            setHomeAsUpIndicator(android.R.drawable.ic_menu_sort_by_size)
        }

        binding.tvNurseName.text = nurseName
        binding.tvAlertCount.text = getString(R.string.nurse_all_clear)
        binding.tvWsStatus.text = getString(R.string.nurse_ws_disconnected)
    }

    // Toolbar hamburger tap
    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        if (item.itemId == android.R.id.home) {
            if (binding.drawerLayout.isDrawerOpen(GravityCompat.START)) {
                binding.drawerLayout.closeDrawer(GravityCompat.START)
            } else {
                binding.drawerLayout.openDrawer(GravityCompat.START)
            }
            return true
        }
        return super.onOptionsItemSelected(item)
    }

    // ── Drawer ────────────────────────────────────────────────────────────────

    private fun setupDrawer() {
        binding.navView.setNavigationItemSelectedListener(this)
    }

    private fun populateDrawerHeader() {
        val header   = binding.navView.getHeaderView(0)
        val fullName = nurseName
        val ward     = UserSession.getWard(this)
            ?: getSharedPreferences(ServerUploader.PREFS_NAME, Context.MODE_PRIVATE)
                .getString("ward", null)

        val initials = fullName.split(" ")
            .filter { it.isNotEmpty() }
            .take(2)
            .joinToString("") { it.first().uppercaseChar().toString() }

        header.findViewById<android.widget.TextView>(R.id.tvAvatarInitials).text = initials
        header.findViewById<android.widget.TextView>(R.id.tvNavName).text        = fullName
        header.findViewById<android.widget.TextView>(R.id.tvNavRole).text        =
            getString(R.string.drawer_role_nurse)
        header.findViewById<android.widget.TextView>(R.id.tvNavWard).text        =
            if (!ward.isNullOrEmpty()) getString(R.string.drawer_ward_fmt, ward)
            else getString(R.string.drawer_ward_unknown)

        // Initial alert count — updated live via refreshDrawerAlertCount()
        header.findViewById<android.widget.TextView>(R.id.tvNavAlerts).text =
            getString(R.string.drawer_alerts_none)
        header.findViewById<android.widget.TextView>(R.id.tvNavWsStatus).text =
            getString(R.string.nurse_ws_disconnected)
    }

    /** Call after allAlerts changes to keep the sidebar alert count in sync. */
    private fun refreshDrawerAlertCount() {
        val header  = binding.navView.getHeaderView(0)
        val tvAlerts = header.findViewById<android.widget.TextView>(R.id.tvNavAlerts)
        val pending = allAlerts.count { !it.attended }
        tvAlerts.text = if (pending > 0)
            resources.getQuantityString(R.plurals.drawer_alerts_pending, pending, pending)
        else
            getString(R.string.drawer_alerts_none)
    }

    /** Mirrors the WS status indicator in the toolbar into the sidebar header. */
    private fun refreshDrawerWsStatus(connected: Boolean) {
        val header = binding.navView.getHeaderView(0)
        header.findViewById<android.widget.TextView>(R.id.tvNavWsStatus).text =
            if (connected) getString(R.string.nurse_ws_connected)
            else getString(R.string.nurse_ws_disconnected)
    }

    override fun onNavigationItemSelected(item: MenuItem): Boolean {
        binding.drawerLayout.closeDrawer(GravityCompat.START)
        return when (item.itemId) {
            R.id.nav_profile  -> { showProfileDialog(); true }
            R.id.nav_settings -> { showSettingsDialog(); true }
            R.id.nav_language -> { chooseLanguage(); true }
            R.id.nav_logout   -> { doLogout(); true }
            else              -> false
        }
    }

    // ── Profile dialog ────────────────────────────────────────────────────────

    // ── Profile dialog (editable) ─────────────────────────────────────────────

    private fun showProfileDialog() {
        val view = layoutInflater.inflate(R.layout.dialog_edit_profile_nurse, null)

        // Populate read-only info
        view.findViewById<TextView>(R.id.tvProfileUsername).text =
            "Logged in as: ${UserSession.getUsername(this) ?: "—"}"

        val etName = view.findViewById<com.google.android.material.textfield.TextInputEditText>(R.id.etFullName)
        val etWard = view.findViewById<com.google.android.material.textfield.TextInputEditText>(R.id.etWard)
        etName.setText(nurseName)
        etWard.setText(
            UserSession.getWard(this)
                ?: getSharedPreferences(ServerUploader.PREFS_NAME, Context.MODE_PRIVATE)
                    .getString("ward", "") ?: ""
        )

        AlertDialog.Builder(this)
            .setTitle(getString(R.string.nurse_profile_edit))
            .setView(view)
            .setPositiveButton(getString(R.string.nurse_save)) { _, _ ->
                val newName = etName.text.toString().trim()
                val newWard = etWard.text.toString().trim()
                if (newName.isBlank()) {
                    Toast.makeText(
                        this,
                        getString(R.string.nurse_name_empty),
                        Toast.LENGTH_SHORT
                    ).show()
                    return@setPositiveButton
                }
                saveNurseProfile(newName, newWard.ifBlank { null })
            }
            .setNegativeButton(getString(R.string.nurse_cancel), null)
            .show()
    }

    private fun saveNurseProfile(fullName: String, ward: String?) {
        val token = UserSession.getToken(this) ?: return

        val body = JSONObject().apply {
            put("full_name", fullName)
            if (ward != null) put("ward", ward)
        }.toString().toRequestBody("application/json".toMediaType())

        val request = Request.Builder()
            .url("${serverUrl.trimEnd('/')}/auth/me")
            .addHeader("Authorization", "Bearer $token")
            .patch(body)
            .build()

        lifecycleScope.launch(Dispatchers.IO) {
            try {
                val resp = http.newCall(request).execute()
                val respBody = resp.body?.string()
                withContext(Dispatchers.Main) {
                    if (resp.isSuccessful && respBody != null) {
                        val json        = JSONObject(respBody)
                        val updatedName = json.optString("full_name").ifEmpty { fullName }
                        val updatedWard = json.optString("ward").ifEmpty { ward }

                        // Persist into session
                        UserSession.save(
                            context    = this@NurseActivity,
                            token      = token,
                            userId     = UserSession.getUserId(this@NurseActivity),
                            fullName   = updatedName,
                            role       = "nurse",
                            roomNumber = null,
                            ward       = updatedWard,
                            username   = UserSession.getUsername(this@NurseActivity)
                        )

                        // Also update ServerUploader prefs for compat
                        if (!updatedWard.isNullOrEmpty()) {
                            getSharedPreferences(ServerUploader.PREFS_NAME, Context.MODE_PRIVATE)
                                .edit().putString("ward", updatedWard).apply()
                        }

                        // Refresh drawer header + toolbar name
                        populateDrawerHeader()
                        binding.tvNurseName.text = updatedName

                        Toast.makeText(
                            this@NurseActivity,
                            getString(R.string.nurse_profile_updated),
                            Toast.LENGTH_SHORT
                        ).show()
                    } else {
                        Toast.makeText(
                            this@NurseActivity,
                            getString(R.string.nurse_update_failed, resp.code),
                            Toast.LENGTH_SHORT
                        ).show()
                    }
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    Toast.makeText(
                        this@NurseActivity,
                        getString(R.string.nurse_network_error, e.message ?: ""),
                        Toast.LENGTH_SHORT
                    ).show()
                }
            }
        }
    }

    // ── Settings ──────────────────────────────────────────────────────────────

    private fun showSettingsDialog() {
        val prefs   = getSharedPreferences(ServerUploader.PREFS_NAME, Context.MODE_PRIVATE)
        val current = prefs.getString(ServerUploader.KEY_SERVER_URL, ServerUploader.DEFAULT_SERVER_URL)

        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            val dp16 = (16 * resources.displayMetrics.density).toInt()
            setPadding(dp16, dp16, dp16, dp16)
        }
        val etUrl = EditText(this).apply {
            hint = getString(R.string.settings_hint_url)
            setText(current)
        }
        layout.addView(etUrl)

        AlertDialog.Builder(this)
            .setTitle(getString(R.string.settings_title))
            .setView(layout)
            .setPositiveButton(getString(R.string.settings_save)) { _, _ ->
                val url = etUrl.text.toString().trim().trimEnd('/')
                if (url.isNotBlank()) {
                    prefs.edit().putString(ServerUploader.KEY_SERVER_URL, url).apply()
                    webSocket?.cancel()
                    connectWebSocket()
                    loadAlerts(showLoading = true)
                }
            }
            .setNegativeButton(getString(R.string.settings_cancel), null)
            .show()
    }

    // ── Logout ────────────────────────────────────────────────────────────────

    private fun doLogout() {
        AlertDialog.Builder(this)
            .setTitle(getString(R.string.drawer_logout))
            .setMessage(getString(R.string.nurse_logout_confirm))
            .setPositiveButton(getString(R.string.nurse_logout)) { _, _ ->
                webSocket?.cancel()
                UserSession.logout(this)
                startActivity(
                    Intent(this, LoginActivity::class.java).apply {
                        flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK
                    }
                )
            }
            .setNegativeButton(getString(R.string.settings_cancel), null)
            .show()
    }

    private fun chooseLanguage() {
        val current = UserSession.getNurseLanguage(this)
        AlertDialog.Builder(this)
            .setTitle(getString(R.string.language_title))
            .setSingleChoiceItems(arrayOf("English", "हिन्दी"), if (current == "hi") 1 else 0) { dialog, which ->
                val tag = if (which == 1) "hi" else "en"
                // 1. Update Android app locale (UI strings)
                AppCompatDelegate.setApplicationLocales(LocaleListCompat.forLanguageTags(tag))
                // 2. Persist locally so getNurseLanguage() returns it immediately
                UserSession.saveNurseLanguage(this, tag)
                android.util.Log.i("NurseActivity",
                    "VOICE_TRANSLATION_DEBUG:\nNURSE_LANGUAGE_CHANGED\nnewLanguage=$tag")
                // 3. PATCH the server — show a warning toast on failure so the
                //    nurse knows their language preference was NOT saved to the
                //    server (which would cause patient voice notes to be in the
                //    wrong language).
                lifecycleScope.launch {
                    val ok = saveNurseLanguageToServer(tag)
                    if (!ok) {
                        withContext(Dispatchers.Main) {
                            Toast.makeText(
                                this@NurseActivity,
                                "⚠️ Language saved locally but server sync failed. " +
                                "Patient voice notes may not be translated correctly. " +
                                "Check your network and try again.",
                                Toast.LENGTH_LONG
                            ).show()
                        }
                    }
                }
                dialog.dismiss()
            }
            .show()
    }

    /**
     * PATCH /auth/me { preferred_language } so the server knows this nurse's
     * selected language for patient→nurse voice-note translation.
     *
     * This is a suspending function — it awaits the server response and returns
     * true on HTTP 200/201, false on any network error or non-success code.
     * The caller is responsible for surfacing failures to the user.
     */
    private suspend fun saveNurseLanguageToServer(language: String): Boolean {
        val t = token ?: run {
            android.util.Log.e("NurseActivity",
                "NURSE_LANG_SYNC: no auth token — cannot PATCH /auth/me")
            return false
        }
        return withContext(Dispatchers.IO) {
            try {
                val body = org.json.JSONObject()
                    .put("preferred_language", language)
                    .toString()
                    .toRequestBody("application/json".toMediaType())
                val req = Request.Builder()
                    .url("${serverUrl.trimEnd('/')}/auth/me")
                    .addHeader("Authorization", "Bearer $t")
                    .patch(body)
                    .build()
                val resp = http.newCall(req).execute()
                val respBody = resp.body?.string() ?: ""
                val success  = resp.isSuccessful

                // Read back the server-confirmed preferred_language so we can
                // verify the value actually persisted (not just that HTTP 200 came).
                val serverLang = runCatching {
                    org.json.JSONObject(respBody).optString("preferred_language", "?")
                }.getOrDefault("?")

                android.util.Log.i("NurseActivity",
                    "VOICE_TRANSLATION_DEBUG:\nNURSE_LANGUAGE_SYNC\n" +
                    "selectedLanguage=$language\n" +
                    "serverConfirmedLanguage=$serverLang\n" +
                    "HTTP=${resp.code}\n" +
                    "success=$success")

                if (!success) {
                    android.util.Log.e("NurseActivity",
                        "NURSE_LANG_SYNC: PATCH /auth/me returned HTTP ${resp.code} " +
                        "body=$respBody")
                }
                success
            } catch (e: Exception) {
                android.util.Log.e("NurseActivity",
                    "NURSE_LANG_SYNC: PATCH /auth/me failed with exception", e)
                false
            }
        }
    }

    /**
     * On startup, push the locally-stored language preference to the server.
     *
     * Retries once after 2 s if the initial attempt fails (handles the common
     * case where the server is briefly unreachable right at app start).
     * Failures are logged but do NOT block the UI — the nurse can still use
     * the app; however, patient voice notes will not be translated to the
     * correct language until the sync succeeds.
     */
    private fun syncNurseLanguageOnStart() {
        val lang = UserSession.getNurseLanguage(this)
        android.util.Log.i("NurseActivity",
            "VOICE_TRANSLATION_DEBUG:\nON_START_LANG_SYNC\nnurseLanguage=$lang")
        lifecycleScope.launch {
            val ok = saveNurseLanguageToServer(lang)
            if (!ok) {
                android.util.Log.w("NurseActivity",
                    "NURSE_LANG_SYNC: initial sync failed — retrying in 2 s")
                delay(2_000)
                val retryOk = saveNurseLanguageToServer(lang)
                if (!retryOk) {
                    android.util.Log.e("NurseActivity",
                        "NURSE_LANG_SYNC: retry also failed. " +
                        "Server may not have preferred_language=$lang. " +
                        "Patient voice notes may be in the wrong language.")
                }
            }
        }
    }

    // ── RecyclerView ──────────────────────────────────────────────────────────

    private fun setupRecyclerView() {
        adapter = AlertAdapter(
            onAck = ::ackAlert,
            onAttend = ::attendAlert,
            onHearSummary = { ttsManager.speakSummary(it) },
            onVoiceReply = ::startVoiceReply,
            onPlayPatientVoice = ::playPatientVoiceNote
        )
        adapter.nurseName = nurseName
        binding.rvAlerts.layoutManager = LinearLayoutManager(this)
        binding.rvAlerts.adapter = adapter
        binding.rvAlerts.setHasFixedSize(false)
    }

    // ── Filter bar ────────────────────────────────────────────────────────────

    private fun setupFilterBar() {
        updateFilterButtons()
        binding.btnFilterAll.setOnClickListener {
            showPendingOnly = false; updateFilterButtons(); submitFilteredList()
        }
        binding.btnFilterPending.setOnClickListener {
            showPendingOnly = true; updateFilterButtons(); submitFilteredList()
        }
        binding.btnRefresh.setOnClickListener { loadAlerts(showLoading = false) }
    }

    private fun updateFilterButtons() {
        binding.btnFilterAll.isEnabled     = showPendingOnly
        binding.btnFilterPending.isEnabled = !showPendingOnly
    }

    // ── Assigned rooms ────────────────────────────────────────────────────────

    /** Fetch this nurse's assigned patients (rooms), then load alerts filtered to them. */
    private fun loadMyRoomsThenAlerts() {
        lifecycleScope.launch(Dispatchers.IO) {
            try {
                val req = Request.Builder()
                    .url("$serverUrl/auth/patients")
                    .addHeader("Authorization", "Bearer $token")
                    .build()
                val body = http.newCall(req).execute().use { r -> r.body?.string() }
                if (body != null) {
                    val arr = JSONArray(body)
                    val rooms = mutableSetOf<String>()
                    for (i in 0 until arr.length()) {
                        val room = arr.getJSONObject(i).optString("room_number").trim()
                        if (room.isNotEmpty()) rooms.add(room)
                    }
                    withContext(Dispatchers.Main) {
                        myRooms.clear(); myRooms.addAll(rooms)
                    }
                }
            } catch (_: Exception) { /* fail-open: myRooms stays empty → show all */ }
            withContext(Dispatchers.Main) { loadAlerts(showLoading = true) }
        }
    }

    /** True if the alert's room belongs to this nurse (or rooms not yet loaded). */
    private fun isMyRoom(roomId: String): Boolean =
        myRooms.isEmpty() || myRooms.contains(roomId.trim())

    // ── Load alerts (HTTP) ────────────────────────────────────────────────────

    private fun loadAlerts(showLoading: Boolean) {
        if (showLoading) {
            binding.progressLoading.visibility = View.VISIBLE
            binding.tvEmpty.visibility         = View.GONE
            binding.rvAlerts.visibility        = View.GONE
        }

        lifecycleScope.launch(Dispatchers.IO) {
            try {
                val req = Request.Builder()
                    .url("$serverUrl/alerts/latest?limit=50&unacked_only=false")
                    .addHeader("Authorization", "Bearer $token")
                    .build()

                val body = http.newCall(req).execute().use { resp ->
                    if (!resp.isSuccessful) {
                        withContext(Dispatchers.Main) { onLoadError(getString(R.string.nurse_http_error, resp.code)) }
                        return@launch
                    }
                    resp.body?.string() ?: run {
                        withContext(Dispatchers.Main) { onLoadError(getString(R.string.nurse_empty_response)) }
                        return@launch
                    }
                }

                val parsed = parseAlertList(body).filter { isMyRoom(it.roomId) }
                withContext(Dispatchers.Main) {
                    binding.progressLoading.visibility = View.GONE
                    allAlerts.clear()
                    allAlerts.addAll(parsed)
                    submitFilteredList()
                    updateCountBadge()
                    refreshDrawerAlertCount()
                    // Announce any pending Urgent/Critical alert not yet spoken
                    // this session (covers app cold-start, WS-down poll
                    // fallback, and reconnect replays). TtsManager dedupes by
                    // (id, priority, escalated) so this never double-speaks.
                    parsed.forEach { ttsManager.announce(it) }
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) { onLoadError(e.message ?: getString(R.string.nurse_unknown_error)) }
            }
        }
    }

    private fun onLoadError(msg: String) {
        binding.progressLoading.visibility = View.GONE
        Snackbar.make(
            binding.root,
            getString(R.string.nurse_load_failed, msg),
            Snackbar.LENGTH_SHORT
        ).show()
        if (allAlerts.isEmpty()) {
            binding.tvEmpty.visibility  = View.VISIBLE
            binding.rvAlerts.visibility = View.GONE
        }
    }

    // ── List rendering ────────────────────────────────────────────────────────

    private fun submitFilteredList() {
        val list = if (showPendingOnly) {
            allAlerts.filter { !it.attended }
        } else {
            allAlerts.toList()
        }

        adapter.submitList(list)
        binding.tvEmpty.visibility  = if (list.isEmpty()) View.VISIBLE else View.GONE
        binding.rvAlerts.visibility = if (list.isEmpty()) View.GONE   else View.VISIBLE
    }
    private fun updateCountBadge() {
        val n = allAlerts.count { !it.attended }
        binding.tvAlertCount.text =
            if (n > 0) {
                getString(R.string.nurse_pending_count, n)
            } else {
                getString(R.string.nurse_all_clear)
            }
    }

    // ── ACK ───────────────────────────────────────────────────────────────────

    private fun ackAlert(alertId: Int, by: String) {
        lifecycleScope.launch(Dispatchers.IO) {
            try {
                val json    = JSONObject().put("ack_by", by).toString()
                val reqBody = json.toRequestBody("application/json".toMediaType())
                val req     = Request.Builder()
                    .url("$serverUrl/alerts/$alertId/ack")
                    .addHeader("Authorization", "Bearer $token")
                    .post(reqBody)
                    .build()

                var attendedAt: String? = null

                val code = http.newCall(req).execute().use { response ->
                    val responseBody = response.body?.string()

                    if (response.isSuccessful && !responseBody.isNullOrBlank()) {
                        try {
                            attendedAt = JSONObject(responseBody)
                                .optString("attended_at")
                                .ifEmpty { null }
                        } catch (_: Exception) {
                            // Keep null if response parsing fails.
                        }
                    }

                    response.code
                }
                                withContext(Dispatchers.Main) {
                    if (code == 200 || code == 201) {
                        val idx = allAlerts.indexOfFirst { it.id == alertId }
                        if (idx >= 0) allAlerts[idx] = allAlerts[idx].copy(acknowledged = true, ackedBy = by)
                        submitFilteredList()
                        updateCountBadge()
                        refreshDrawerAlertCount()
                        Snackbar.make(binding.root, getString(R.string.nurse_ack_success), Snackbar.LENGTH_SHORT).show()
                    } else {
                        submitFilteredList()
                        Snackbar.make(binding.root, getString(R.string.nurse_ack_failed), Snackbar.LENGTH_SHORT).show()
                    }
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    submitFilteredList()
                    Snackbar.make(binding.root, getString(R.string.nurse_ack_failed), Snackbar.LENGTH_SHORT).show()
                }
            }
        }
    }

    // ── ATTEND ────────────────────────────────────────────────────────────────
    // Separate from ACK: ATTEND means the patient has actually been cared for,
    // and is the ONLY action that stops Urgent→Critical auto-escalation on the
    // backend. Never triggered automatically by ackAlert().

   private fun attendAlert(alertId: Int, by: String) {
        lifecycleScope.launch(Dispatchers.IO) {
            try {
                val json = JSONObject().put("attended_by", by).toString()
                val reqBody = json.toRequestBody("application/json".toMediaType())

                val req = Request.Builder()
                    .url("$serverUrl/alerts/$alertId/attend")
                    .addHeader("Authorization", "Bearer $token")
                    .post(reqBody)
                    .build()

                var attendedAt: String? = null

                val code = http.newCall(req).execute().use { response ->
                    val responseBody = response.body?.string()

                    if (response.isSuccessful && !responseBody.isNullOrBlank()) {
                        try {
                            attendedAt = JSONObject(responseBody)
                                .optString("attended_at")
                                .ifEmpty { null }
                        } catch (_: Exception) {
                            // Keep null if response parsing fails.
                        }
                    }

                    response.code
                }
                withContext(Dispatchers.Main) {
                    if (code == 200 || code == 201) {
                        val idx = allAlerts.indexOfFirst { it.id == alertId }

                        if (idx >= 0) {
                            allAlerts[idx] = allAlerts[idx].copy(
                                attended = true,
                                attendedBy = by,
                                attendedAt = attendedAt
                            )
                        }

                        submitFilteredList()
                    } else {
                        Toast.makeText(
                            this@NurseActivity,
                            getString(R.string.nurse_attend_failed_code, code),
                            Toast.LENGTH_SHORT
                        ).show()
                    }
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    Toast.makeText(
                        this@NurseActivity,
                        getString(R.string.nurse_attend_failed_error, e.message ?: ""),
                        Toast.LENGTH_SHORT
                    ).show()
                }
            }
        }
    }
    // ── Voice reply ─────────────────────────────────────────────────────────

    private fun startVoiceReply(alert: AlertModel) {
        pendingVoiceReply = alert
        if (androidx.core.content.ContextCompat.checkSelfPermission(
                this, android.Manifest.permission.RECORD_AUDIO
            ) == android.content.pm.PackageManager.PERMISSION_GRANTED
        ) {
            showVoiceReplyDialog(alert)
        } else {
            requestVoiceNotePermission.launch(android.Manifest.permission.RECORD_AUDIO)
        }
    }

    private fun showVoiceReplyDialog(alert: AlertModel) {
        val recorder = VoiceNoteRecorder(this)
        voiceNoteRecorder = recorder

        val status = TextView(this).apply {
            text = getString(R.string.nurse_voice_ready, alert.roomId)
            setPadding(24, 12, 24, 12)
        }
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            addView(status)
        }

        val dialog = AlertDialog.Builder(this)
            .setTitle(getString(R.string.nurse_voice_reply_title, alert.roomId))
            .setView(box)
            .setNegativeButton(getString(R.string.nurse_cancel)) { _, _ -> recorder.stop() }
            .setPositiveButton(getString(R.string.nurse_start_recording), null)
            .create()

        dialog.setOnShowListener {
            val button = dialog.getButton(AlertDialog.BUTTON_POSITIVE)
            button.setOnClickListener {
                if (!recorder.isRecording()) {
                    status.text = getString(R.string.nurse_recording)
                    button.text = getString(R.string.nurse_stop_send)
                    recorder.setOnFinished { wav, duration ->
                        if (wav.size < 44) {
                            status.text = getString(R.string.nurse_no_audio)
                            button.isEnabled = true
                            return@setOnFinished
                        }
                        button.isEnabled = false
                        status.text = getString(R.string.nurse_sending_voice)
                        lifecycleScope.launch {
                            android.util.Log.i("NurseActivity", "Voice reply: recording stopped, wav=${wav.size} bytes, uploading…")
                            val noteId = ServerUploader(this@NurseActivity)
                                .uploadVoiceNote(
                                    wav, alert.roomId, alert.id, duration,
                                    AppCompatDelegate.getApplicationLocales().get(0)?.language ?: "en"
                                )
                            withContext(Dispatchers.Main) {
                                if (noteId != null) {
                                    android.util.Log.i("NurseActivity", "Voice reply sent successfully — noteId=$noteId")
                                    Toast.makeText(
                                        this@NurseActivity,
                                        getString(R.string.nurse_voice_reply_sent, alert.roomId),
                                        Toast.LENGTH_SHORT
                                    ).show()
                                    dialog.dismiss()
                                } else {
                                    android.util.Log.e("NurseActivity", "Voice reply upload failed")
                                    status.text = getString(R.string.nurse_voice_upload_failed)
                                    button.isEnabled = true
                                    button.text = getString(R.string.nurse_start_recording)
                                }
                            }
                        }
                    }
                    recorder.start(lifecycleScope)
                } else {
                    recorder.stop()
                }
            }
        }
        dialog.setOnDismissListener { recorder.stop(); voiceNoteRecorder = null; pendingVoiceReply = null }
        dialog.show()
    }

    private fun playPatientVoiceNote(alert: AlertModel) {
        lifecycleScope.launch(Dispatchers.IO) {
            val json = ServerUploader(this@NurseActivity).getVoiceNotes(alert.roomId) ?: run {
                withContext(Dispatchers.Main) {
                    Toast.makeText(this@NurseActivity, getString(R.string.nurse_no_patient_voice), Toast.LENGTH_SHORT).show()
                }
                return@launch
            }
            try {
                val arr = org.json.JSONArray(json)
                var noteId: Int? = null
                var translatedText = ""
                var originalText = ""
                var targetLanguage = "en"
                var sourceLanguage = "en"
                for (i in 0 until arr.length()) {
                    val item = arr.getJSONObject(i)
                    if (item.optString("sender_role") == "patient" &&
                        item.optInt("alert_id", -1) == alert.id) {
                        noteId = item.optInt("id")
                        translatedText = item.optString("translated_text", "")
                        originalText = item.optString("original_text", "")
                        targetLanguage = item.optString("target_language", "en")
                        sourceLanguage = item.optString("source_language", "en")
                        android.util.Log.d("NurseActivity", "Patient voice note found: noteId=$noteId lang=$targetLanguage alertId=${alert.id}")
                        break
                    }
                }
                if (noteId == null) {
                    withContext(Dispatchers.Main) {
                        Toast.makeText(this@NurseActivity, getString(R.string.nurse_no_patient_voice), Toast.LENGTH_SHORT).show()
                    }
                    return@launch
                }

                withContext(Dispatchers.Main) {
                    if (translatedText.isNotBlank()) {
                        val nurseTargetLang = targetLanguage.ifBlank { "en" }
                        android.util.Log.i("NurseActivity",
                            "VOICE_TRANSLATION_DEBUG:\nNURSE_RECEIVED\n" +
                            "voiceNoteId=$noteId\n" +
                            "sourceLanguage=$sourceLanguage\n" +
                            "targetLanguage=$nurseTargetLang\n" +
                            "originalText=$originalText\n" +
                            "translatedText=$translatedText")
                        val ttsLocale = if (nurseTargetLang == "hi") "hi-IN" else "en-IN"
                        android.util.Log.i("NurseActivity",
                            "VOICE_TRANSLATION_DEBUG:\nNURSE_TTS\n" +
                            "voiceNoteId=$noteId\n" +
                            "targetLanguage=$nurseTargetLang\n" +
                            "ttsLocale=$ttsLocale\n" +
                            "text=$translatedText")
                        Toast.makeText(
                            this@NurseActivity,
                            "Translated: $translatedText",
                            Toast.LENGTH_LONG
                        ).show()
                        ttsManager.speakVoiceNote(translatedText, nurseTargetLang, noteId)
                    } else {
                        Toast.makeText(this@NurseActivity, "Voice message is empty or untranslated", Toast.LENGTH_SHORT).show()
                    }
                }
            } catch (e: Exception) {
                android.util.Log.e("NurseActivity", "playPatientVoiceNote error", e)
            }
        }
    }

    // ── WebSocket ─────────────────────────────────────────────────────────────

    private fun connectWebSocket() {
        val wsUrl = serverUrl
            .replace("http://", "ws://")
            .replace("https://", "wss://") + "/ws/nurse"

        val req = Request.Builder()
            .url(wsUrl)
            .addHeader("Authorization", "Bearer $token")
            .build()

        webSocket = wsHttp.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(ws: WebSocket, response: Response) {
                runOnUiThread {
                    binding.tvWsStatus.text = getString(R.string.nurse_ws_connected)
                    refreshDrawerWsStatus(true)
                }
            }

            override fun onMessage(ws: WebSocket, text: String) {
                // Reply to server keepalive pings immediately
                if (text == "ping") {
                    ws.send("pong")
                    return
                }
                try {
                    val j = JSONObject(text)
                    val event = j.optString("event")

                    if (event == "new_alert") {
                        val alert = alertFromJson(j, idKey = "alert_id")

                        // Ignore alerts for rooms not assigned to this nurse.
                        if (!isMyRoom(alert.roomId)) return

                        runOnUiThread {
                            allAlerts.removeAll { it.id == alert.id }
                            allAlerts.add(0, alert)

                            submitFilteredList()
                            updateCountBadge()
                            refreshDrawerAlertCount()

                            binding.rvAlerts.scrollToPosition(0)

                            // Speak new Urgent/Critical alerts.
                            // TtsManager handles deduplication.
                            ttsManager.announce(alert)
                        }
                    }

                    if (event == "alert_updated") {
                        val updated = alertFromJson(j, idKey = "alert_id")

                        // Ignore alerts for rooms not assigned to this nurse.
                        if (!isMyRoom(updated.roomId)) return

                        runOnUiThread {
                            val idx = allAlerts.indexOfFirst { it.id == updated.id }

                            if (idx >= 0) {
                                allAlerts[idx] = updated
                            } else {
                                // If this alert wasn't loaded locally yet, add it.
                                allAlerts.add(0, updated)
                            }

                            submitFilteredList()
                            updateCountBadge()
                            refreshDrawerAlertCount()

                            // Announce an escalation to Critical.
                            ttsManager.announce(updated)
                        }
                    }
                } catch (_: Exception) {}
            }
            override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
                runOnUiThread {
                    binding.tvWsStatus.text = getString(R.string.nurse_ws_disconnected)
                    refreshDrawerWsStatus(false)
                }
                scheduleWsReconnect()
            }

            override fun onClosed(ws: WebSocket, code: Int, reason: String) {
                runOnUiThread {
                    binding.tvWsStatus.text = getString(R.string.nurse_ws_disconnected)
                    refreshDrawerWsStatus(false)
                }
                scheduleWsReconnect()
            }
        })
    }

    private fun scheduleWsReconnect() {
        wsReconnectJob?.cancel()
        wsReconnectJob = lifecycleScope.launch {
            delay(1_000)   // reduced from 5s: minimises window where updates
                           // only reach nurse via the 15s poll fallback
            if (isActive) {
                connectWebSocket()
                // Immediate catch-up poll — picks up any alerts that were
                // broadcast while the WS was disconnected, rather than
                // waiting for the next 15s poll cycle.
                loadAlerts(showLoading = false)
            }
        }
    }

    // ── HTTP poll fallback ────────────────────────────────────────────────────

    private fun startPollLoop() {
        pollJob = lifecycleScope.launch {
            while (isActive) {
                delay(15_000)
                if (isActive) loadAlerts(showLoading = false)
            }
        }
    }

    // ── JSON helpers ──────────────────────────────────────────────────────────

    private fun parseAlertList(body: String): List<AlertModel> {
        val root = JSONObject(body)
        val arr: JSONArray = root.getJSONArray("alerts")
        return (0 until arr.length()).map { i -> alertFromJson(arr.getJSONObject(i), idKey = "id") }
    }

    /**
     * Builds an [AlertModel] from either payload shape the backend sends:
     *   - GET /alerts/latest and GET /alerts/{id} → AlertResponse, keyed "id"
     *   - the /ws/nurse push (new_alert)          → WsAlertPayload, keyed "alert_id"
     * Every other field name is identical between the two, per models.py.
     *
     * All escalation-related fields use `opt*` with safe defaults so older
     * cached payloads (or a server not yet updated) don't crash parsing —
     * "Maintain compatibility with older payloads where practical."
     */
    private fun alertFromJson(j: JSONObject, idKey: String): AlertModel {
        val priority = j.getString("priority")
        return AlertModel(
            id                 = j.getInt(idKey),
            roomId             = j.getString("room_id"),
            patientName        = j.optString("patient_name", "Patient"),
            language            = j.optString("language", "en"),
            nlpSummary          = j.optString("nlp_summary", ""),
            priority           = priority,
            initialPriority    = j.optString("initial_priority", priority),
            intent             = j.getString("intent"),
            distressScore      = j.optDouble("distress_score", 0.0).toFloat(),
            transcript         = j.getString("transcript"),
            createdAt          = j.optString("created_at", ""),
            acknowledged       = j.optBoolean("acknowledged", false),
            ackedBy            = j.optString("ack_by").ifEmpty { null },
            attended           = j.optBoolean("attended", false),
            attendedBy         = j.optString("attended_by").ifEmpty { null },
            attendedAt         = j.optString("attended_at").ifEmpty { null },
            escalationDeadline = j.optString("escalation_deadline").ifEmpty { null },
            escalated          = j.optBoolean("escalated", false),
            patientAcknowledged = j.optBoolean("patient_acknowledged", false),
            patientAckAt         = j.optString("patient_ack_at").ifEmpty { null }
        )
    }
}
