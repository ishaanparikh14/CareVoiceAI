package com.carevoice.app

import android.content.Context
import android.content.Intent
import android.media.AudioManager
import android.media.ToneGenerator
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

    // Text-to-speech announcements for Critical alerts (en/hi).
    private lateinit var tts: NurseTts
    private val voicePrefs get() = getSharedPreferences(ServerUploader.PREFS_NAME, Context.MODE_PRIVATE)

    private val allAlerts = mutableListOf<AlertModel>()
    private var showUnackedOnly = false

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

    // ── Lifecycle ─────────────────────────────────────────────────────────────

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityNurseBinding.inflate(layoutInflater)
        setContentView(binding.root)

        // Voice announcements — load saved prefs (default: on, English).
        // Kannada TTS removed — migrate any previously-saved 'kn' choice to English.
        var savedLang = voicePrefs.getString(KEY_VOICE_LANG, "en") ?: "en"
        if (savedLang != "en" && savedLang != "hi") {
            savedLang = "en"
            voicePrefs.edit().putString(KEY_VOICE_LANG, "en").apply()
        }
        tts = NurseTts(this).apply {
            enabled = voicePrefs.getBoolean(KEY_VOICE_ON, true)
            setLanguage(savedLang)
        }

        setupToolbar()
        setupDrawer()
        populateDrawerHeader()
        setupRecyclerView()
        setupFilterBar()

        // Load this nurse's assigned rooms first, then alerts (filtered to them).
        loadMyRoomsThenAlerts()
        connectWebSocket()
        startPollLoop()

        // Connect call signaling so the nurse can place/receive real-time calls.
        ensureCallSignaling()
    }

    override fun onResume() {
        super.onResume()
        ensureCallSignaling()
    }

    override fun onDestroy() {
        super.onDestroy()
        pollJob?.cancel()
        wsReconnectJob?.cancel()
        webSocket?.cancel()
        if (::tts.isInitialized) tts.shutdown()
        try { toneGen.release() } catch (_: Exception) {}
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

        binding.tvNurseName.text  = nurseName
        binding.tvAlertCount.text = "0 pending"
        binding.tvWsStatus.text   = getString(R.string.nurse_ws_disconnected)
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
        val pending  = allAlerts.count { !it.acknowledged }
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
            R.id.nav_voice    -> { showVoiceDialog(); true }
            R.id.nav_settings -> { showSettingsDialog(); true }
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
            .setTitle("Edit Profile")
            .setView(view)
            .setPositiveButton("Save") { _, _ ->
                val newName = etName.text.toString().trim()
                val newWard = etWard.text.toString().trim()
                if (newName.isBlank()) {
                    Toast.makeText(this, "Name cannot be empty", Toast.LENGTH_SHORT).show()
                    return@setPositiveButton
                }
                saveNurseProfile(newName, newWard.ifBlank { null })
            }
            .setNegativeButton("Cancel", null)
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

                        Toast.makeText(this@NurseActivity, "Profile updated", Toast.LENGTH_SHORT).show()
                    } else {
                        Toast.makeText(this@NurseActivity, "Update failed (${resp.code})", Toast.LENGTH_SHORT).show()
                    }
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    Toast.makeText(this@NurseActivity, "Network error: ${e.message}", Toast.LENGTH_SHORT).show()
                }
            }
        }
    }

    // ── Voice announcements ─────────────────────────────────────────────────

    private fun showVoiceDialog() {
        val dp = resources.displayMetrics.density
        val pad = (16 * dp).toInt()

        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
        }

        // Enable/disable checkbox
        val cb = android.widget.CheckBox(this).apply {
            text = "Speak Critical alerts aloud"
            isChecked = tts.enabled
        }
        layout.addView(cb)

        // Language spinner (Kannada TTS removed)
        val labels = arrayOf("English", "हिंदी (Hindi)")
        val codes  = arrayOf("en", "hi")
        val spinner = android.widget.Spinner(this).apply {
            adapter = android.widget.ArrayAdapter(
                this@NurseActivity, android.R.layout.simple_spinner_dropdown_item, labels
            )
            setSelection(codes.indexOf(tts.lang).coerceAtLeast(0))
        }
        val langLabel = TextView(this).apply {
            text = "Announcement language"
            setPadding(0, pad, 0, (4 * dp).toInt())
        }
        layout.addView(langLabel)
        layout.addView(spinner)

        AlertDialog.Builder(this)
            .setTitle("Voice Alerts")
            .setView(layout)
            .setPositiveButton("Save") { _, _ ->
                val on   = cb.isChecked
                val code = codes[spinner.selectedItemPosition]
                tts.enabled = on
                tts.setLanguage(code)
                voicePrefs.edit()
                    .putBoolean(KEY_VOICE_ON, on)
                    .putString(KEY_VOICE_LANG, code)
                    .apply()
            }
            .setNegativeButton(getString(R.string.settings_cancel), null)
            .show()
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
            .setMessage("Are you sure you want to log out?")
            .setPositiveButton("Log out") { _, _ ->
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

    // ── RecyclerView ──────────────────────────────────────────────────────────

    private fun setupRecyclerView() {
        adapter = AlertAdapter(onAck = ::ackAlert, onCall = ::callPatientInRoom)
        adapter.nurseName = nurseName
        binding.rvAlerts.layoutManager = LinearLayoutManager(this)
        binding.rvAlerts.adapter = adapter
        binding.rvAlerts.setHasFixedSize(false)
    }

    // ── Real-time voice call to a patient ───────────────────────────────────---

    /** Maps room number → patient username, filled from /auth/patients. */
    private val roomToPatient = mutableMapOf<String, String>()
    private val roomToPatientName = mutableMapOf<String, String>()

    private fun ensureCallSignaling() {
        val tok = token ?: return
        CallSession.ensureSignaling(this, serverUrl, tok)
    }

    /** Place a real-time WebRTC voice call to the patient in [roomId]. */
    private fun callPatientInRoom(roomId: String) {
        val patientUser = roomToPatient[roomId.trim()]
        if (patientUser == null) {
            android.widget.Toast.makeText(this, "No patient mapped to room $roomId", android.widget.Toast.LENGTH_SHORT).show()
            return
        }
        if (androidx.core.content.ContextCompat.checkSelfPermission(
                this, android.Manifest.permission.RECORD_AUDIO
            ) != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            micPermissionForCall.launch(android.Manifest.permission.RECORD_AUDIO)
            return
        }
        ensureCallSignaling()
        val name = roomToPatientName[roomId.trim()] ?: "Room $roomId"
        CallSession.placeCall(to = patientUser, displayName = name, roomId = roomId)
    }

    private val micPermissionForCall =
        registerForActivityResult(androidx.activity.result.contract.ActivityResultContracts.RequestPermission()) { granted ->
            if (!granted) android.widget.Toast.makeText(
                this, getString(R.string.snack_mic_denied), android.widget.Toast.LENGTH_LONG
            ).show()
        }

    // ── Filter bar ────────────────────────────────────────────────────────────

    private fun setupFilterBar() {
        updateFilterButtons()
        binding.btnFilterAll.setOnClickListener {
            showUnackedOnly = false; updateFilterButtons(); submitFilteredList()
        }
        binding.btnFilterPending.setOnClickListener {
            showUnackedOnly = true; updateFilterButtons(); submitFilteredList()
        }
        binding.btnRefresh.setOnClickListener { loadAlerts(showLoading = false) }
    }

    private fun updateFilterButtons() {
        binding.btnFilterAll.isEnabled     = showUnackedOnly
        binding.btnFilterPending.isEnabled = !showUnackedOnly
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
                    val roomUser = mutableMapOf<String, String>()
                    val roomName = mutableMapOf<String, String>()
                    for (i in 0 until arr.length()) {
                        val obj  = arr.getJSONObject(i)
                        val room = obj.optString("room_number").trim()
                        if (room.isNotEmpty()) {
                            rooms.add(room)
                            obj.optString("username").trim().takeIf { it.isNotEmpty() }?.let { roomUser[room] = it }
                            obj.optString("full_name").trim().takeIf { it.isNotEmpty() }?.let { roomName[room] = it }
                        }
                    }
                    withContext(Dispatchers.Main) {
                        myRooms.clear(); myRooms.addAll(rooms)
                        roomToPatient.clear(); roomToPatient.putAll(roomUser)
                        roomToPatientName.clear(); roomToPatientName.putAll(roomName)
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
                        withContext(Dispatchers.Main) { onLoadError("HTTP ${resp.code}") }
                        return@launch
                    }
                    resp.body?.string() ?: run {
                        withContext(Dispatchers.Main) { onLoadError("Empty response") }
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
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) { onLoadError(e.message ?: "Unknown error") }
            }
        }
    }

    private fun onLoadError(msg: String) {
        binding.progressLoading.visibility = View.GONE
        Snackbar.make(binding.root, "Load failed: $msg", Snackbar.LENGTH_SHORT).show()
        if (allAlerts.isEmpty()) {
            binding.tvEmpty.visibility  = View.VISIBLE
            binding.rvAlerts.visibility = View.GONE
        }
    }

    // ── List rendering ────────────────────────────────────────────────────────

    private fun submitFilteredList() {
        val list = if (showUnackedOnly) allAlerts.filter { !it.acknowledged }
                   else allAlerts.toList()
        adapter.submitList(list)
        binding.tvEmpty.visibility  = if (list.isEmpty()) View.VISIBLE else View.GONE
        binding.rvAlerts.visibility = if (list.isEmpty()) View.GONE   else View.VISIBLE
    }

    private fun updateCountBadge() {
        val n = allAlerts.count { !it.acknowledged }
        binding.tvAlertCount.text = if (n > 0) "$n pending" else "All clear ✓"
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

                val code = http.newCall(req).execute().use { it.code }
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

    // ── Alert sound (all priorities; distinct emergency signal) ─────────────

    private val chimedIds = HashSet<Int>()
    private val toneGen: ToneGenerator by lazy {
        ToneGenerator(AudioManager.STREAM_NOTIFICATION, 90)
    }

    /**
     * Plays a sound for a new unacknowledged alert. Critical gets a distinct,
     * insistent emergency signal; Urgent/Routine get softer notification tones.
     * Deduped per alert id so re-renders / polls don't replay.
     */
    private fun chimeForAlert(alertId: Int, priority: String) {
        if (!chimedIds.add(alertId)) return
        try {
            when (priority) {
                "Critical" -> {
                    // Distinct emergency signal — urgent repeating tone.
                    toneGen.startTone(ToneGenerator.TONE_CDMA_EMERGENCY_RINGBACK, 1200)
                }
                "Urgent" -> toneGen.startTone(ToneGenerator.TONE_PROP_BEEP2, 400)
                else     -> toneGen.startTone(ToneGenerator.TONE_PROP_BEEP, 250)
            }
        } catch (_: Exception) {}
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
                    // "new_alert" = freshly created; "alert_updated" = server-side
                    // change (e.g. Urgent auto-escalated to Critical).
                    if (event != "new_alert" && event != "alert_updated") return

                    val id = j.getInt("alert_id")
                    // Preserve acknowledged state on updates (server payload omits it).
                    val prior = allAlerts.firstOrNull { it.id == id }
                    val alert = AlertModel(
                        id            = id,
                        roomId        = j.getString("room_id"),
                        priority      = j.getString("priority"),
                        intent        = j.getString("intent"),
                        distressScore = j.optDouble("distress_score", 0.0).toFloat(),
                        transcript    = j.getString("transcript"),
                        createdAt     = j.getString("created_at"),
                        acknowledged  = prior?.acknowledged ?: false,
                        ackedBy       = prior?.ackedBy,
                        escalated     = j.optBoolean("escalated", false),
                        patientName   = j.optString("patient_name").ifEmpty { null },
                        summary       = j.optString("summary").ifEmpty { null },
                        emotion       = j.optString("emotion").ifEmpty { null }
                    )
                    // Ignore alerts for rooms not assigned to this nurse.
                    if (!isMyRoom(alert.roomId)) return
                    runOnUiThread {
                        allAlerts.removeAll { it.id == alert.id }
                        allAlerts.add(0, alert)
                        submitFilteredList()
                        updateCountBadge()
                        refreshDrawerAlertCount()
                        binding.rvAlerts.scrollToPosition(0)
                        // Every alert makes a sound; Critical gets a distinct
                        // emergency signal. Deduped per id via chimedIds.
                        if (!alert.acknowledged) chimeForAlert(alert.id, alert.priority)
                        // Speak the summary for every unacknowledged alert (TTS).
                        tts.maybeAnnounce(
                            alertId      = alert.id,
                            priority     = alert.priority,
                            roomId       = alert.roomId,
                            intent       = alert.intent,
                            escalated    = alert.escalated,
                            acknowledged = alert.acknowledged,
                            summary      = alert.summary,
                            patientName  = alert.patientName
                        )
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
            delay(5_000)
            if (isActive) connectWebSocket()
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
        return (0 until arr.length()).map { i ->
            val j = arr.getJSONObject(i)
            AlertModel(
                id            = j.getInt("id"),
                roomId        = j.getString("room_id"),
                priority      = j.getString("priority"),
                intent        = j.getString("intent"),
                distressScore = j.optDouble("distress_score", 0.0).toFloat(),
                transcript    = j.getString("transcript"),
                createdAt     = j.getString("created_at"),
                acknowledged  = j.getBoolean("acknowledged"),
                ackedBy       = j.optString("ack_by").ifEmpty { null },
                escalated     = j.optBoolean("escalated", false),
                patientName   = j.optString("patient_name").ifEmpty { null },
                summary       = j.optString("summary").ifEmpty { null },
                emotion       = j.optString("emotion").ifEmpty { null }
            )
        }
    }

    companion object {
        private const val KEY_VOICE_ON   = "voice_alerts_on"
        private const val KEY_VOICE_LANG = "voice_alerts_lang"
    }
}
