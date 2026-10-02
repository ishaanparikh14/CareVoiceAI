package com.carevoice.app

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.media.MediaPlayer
import android.view.MenuItem
import android.view.View
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.view.GravityCompat
import androidx.appcompat.app.AppCompatDelegate
import androidx.core.os.LocaleListCompat
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.lifecycleScope
import com.carevoice.app.databinding.ActivityMainBinding
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
import org.json.JSONObject
import java.util.concurrent.TimeUnit

class MainActivity : AppCompatActivity(), NavigationView.OnNavigationItemSelectedListener {

    private lateinit var binding: ActivityMainBinding
    private lateinit var viewModel: MainViewModel
    private var voiceNotePollJob: Job? = null
    private var latestNurseVoiceNoteId: Int? = null
    private var latestNurseVoiceNoteText: String? = null
    private var latestNurseVoiceNoteLang: String? = null
    private var latestNurseVoiceNoteOrig: String? = null
    private var patientStatusPollJob: Job? = null
    private var patientVoiceNoteRecorder: VoiceNoteRecorder? = null
    private var latestAlertId: Int? = null
    private lateinit var ttsManager: TtsManager

    private val http = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .build()

    // ── Permission launcher ───────────────────────────────────────────────────

    private val requestMicPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (granted) viewModel.startListening()
            else Snackbar.make(binding.root, getString(R.string.snack_mic_denied), Snackbar.LENGTH_LONG).show()
        }

    private val requestVoiceNotePermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (granted) startPatientVoiceNote()
            else Snackbar.make(binding.root, getString(R.string.snack_mic_denied), Snackbar.LENGTH_LONG).show()
        }

    // ── Lifecycle ─────────────────────────────────────────────────────────────

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding   = ActivityMainBinding.inflate(layoutInflater)
        viewModel = ViewModelProvider(this)[MainViewModel::class.java]
        setContentView(binding.root)
        
        ttsManager = TtsManager(this)

        val patientName = UserSession.getFullName(this) ?: "Patient"
        val roomNumber  = UserSession.getRoomNumber(this) ?: "—"
        binding.tvAppTitle.text = "CareVoice AI | $patientName (Room $roomNumber)"

        setupDrawer()
        populateDrawerHeader()
        setupButtons()
        observeViewModel()
        viewModel.ensureModelReady()
        setupVoiceNoteUi()
        startPatientStatusPolling()
        startVoiceNotePolling()
    }

    override fun onDestroy() {
        voiceNotePollJob?.cancel()
        patientStatusPollJob?.cancel()
        patientVoiceNoteRecorder?.stop()
        ttsManager.shutdown()
        super.onDestroy()
    }

    private fun setupVoiceNoteUi() {
        binding.btnPlayVoiceNote.setOnClickListener { playLatestNurseVoiceNote() }
        binding.btnSendVoiceNote.setOnClickListener { startPatientVoiceNote() }
        // Patient ACK button is never shown — the patient sees status only.
        binding.btnPatientAck.visibility = View.GONE
    }

    private fun startPatientStatusPolling() {
        patientStatusPollJob?.cancel()
        patientStatusPollJob = lifecycleScope.launch {
            while (isActive) {
                fetchLatestPatientAlert()
                delay(2000)
            }
        }
    }

    private suspend fun fetchLatestPatientAlert() {
        val room = UserSession.getRoomNumber(this) ?: return
        val token = UserSession.getToken(this) ?: return
        val prefs = getSharedPreferences(ServerUploader.PREFS_NAME, Context.MODE_PRIVATE)
        val base = prefs.getString(ServerUploader.KEY_SERVER_URL, ServerUploader.DEFAULT_SERVER_URL)
            ?: ServerUploader.DEFAULT_SERVER_URL
        val request = Request.Builder()
            .url("${base.trimEnd('/')}/alerts/patient/latest?room_id=${java.net.URLEncoder.encode(room, "UTF-8")}")
            .addHeader("Authorization", "Bearer $token")
            .get()
            .build()

        withContext(Dispatchers.IO) {
            try {
                http.newCall(request).execute().use { response ->
                    if (!response.isSuccessful) return@use
                    val body = response.body?.string().orEmpty()
                    if (body.isBlank() || body == "null") return@use
                    val j = JSONObject(body)
                    withContext(Dispatchers.Main) {
                        renderPatientAlertStatus(j)
                    }
                }
            } catch (_: Exception) {
                // Poll again on the next interval.
            }
        }
    }

    private fun renderPatientAlertStatus(j: JSONObject) {
        latestAlertId = j.optInt("id", 0).takeIf { it > 0 }
        binding.cardRequestStatus.visibility = View.VISIBLE

        val patientName = j.optString("patient_name", UserSession.getFullName(this) ?: "Patient")
        val priority = j.optString("priority", "Routine")
        val acknowledged = j.optBoolean("acknowledged", false)
        val attended = j.optBoolean("attended", false)
        val ackBy = j.optString("ack_by").ifEmpty { "Nurse" }
        val summary = j.optString("nlp_summary").ifEmpty {
            j.optString("transcript", "")
        }

        // Show a clear status progression — patient sees read-only status only.
        binding.tvRequestStatusTitle.text = when {
            attended -> getString(R.string.patient_status_attended)
            acknowledged -> getString(R.string.patient_status_acknowledged)
            else -> getString(R.string.patient_status_sent)
        }
        binding.tvRequestStatusDetail.text = getString(
            R.string.patient_status_detail,
            patientName,
            priority,
            if (acknowledged) ackBy else getString(R.string.patient_status_waiting)
        )
        binding.tvRequestSummary.text = summary

        // Patient NEVER gets an ACK button — only the nurse can ACK.
        // The patient sees the nurse's action reflected as a status update.
        binding.btnPatientAck.visibility = View.GONE
        binding.tvPatientAcked.visibility = View.GONE
    }

    private fun acknowledgePatientRequest() {
        val alertId = latestAlertId ?: return
        val token = UserSession.getToken(this) ?: return
        val prefs = getSharedPreferences(ServerUploader.PREFS_NAME, Context.MODE_PRIVATE)
        val base = prefs.getString(ServerUploader.KEY_SERVER_URL, ServerUploader.DEFAULT_SERVER_URL)
            ?: ServerUploader.DEFAULT_SERVER_URL

        lifecycleScope.launch(Dispatchers.IO) {
            try {
                val request = Request.Builder()
                    .url("${base.trimEnd('/')}/alerts/$alertId/patient-ack")
                    .addHeader("Authorization", "Bearer $token")
                    .post(ByteArray(0).toRequestBody(null))
                    .build()
                http.newCall(request).execute().use { response ->
                    withContext(Dispatchers.Main) {
                        if (response.isSuccessful) {
                            Toast.makeText(this@MainActivity, getString(R.string.patient_ack_success), Toast.LENGTH_SHORT).show()
                            binding.btnPatientAck.visibility = View.GONE
                            binding.tvPatientAcked.visibility = View.VISIBLE
                        }
                    }
                }
            } catch (_: Exception) {}
        }
    }

    private fun startPatientVoiceNote() {
        if (latestAlertId == null) {
            Toast.makeText(this, getString(R.string.patient_voice_need_request), Toast.LENGTH_SHORT).show()
            return
        }
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestVoiceNotePermission.launch(Manifest.permission.RECORD_AUDIO)
            return
        }

        val recorder = VoiceNoteRecorder(this)
        patientVoiceNoteRecorder = recorder
        val status = TextView(this).apply {
            text = getString(R.string.voice_note_ready)
            setPadding(24, 12, 24, 12)
        }
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            addView(status)
        }
        val dialog = AlertDialog.Builder(this)
            .setTitle(getString(R.string.patient_voice_note_title))
            .setView(box)
            .setNegativeButton(getString(R.string.settings_cancel)) { _, _ -> recorder.stop() }
            .setPositiveButton(getString(R.string.voice_note_start), null)
            .create()

        dialog.setOnShowListener {
            val button = dialog.getButton(AlertDialog.BUTTON_POSITIVE)
            button.setOnClickListener {
                if (!recorder.isRecording()) {
                    status.text = getString(R.string.voice_note_recording)
                    button.text = getString(R.string.voice_note_stop_send)
                    recorder.setOnFinished { wav, duration ->
                        button.isEnabled = false
                        status.text = getString(R.string.voice_note_sending)
                        lifecycleScope.launch {
                            val noteId = ServerUploader(this@MainActivity)
                                .uploadVoiceNote(
                                    wav,
                                    UserSession.getRoomNumber(this@MainActivity) ?: return@launch,
                                    latestAlertId,
                                    duration,
                                    currentAppLanguage()
                                )
                            withContext(Dispatchers.Main) {
                                if (noteId != null) {
                                    Toast.makeText(this@MainActivity, getString(R.string.voice_note_sent), Toast.LENGTH_SHORT).show()
                                    dialog.dismiss()
                                } else {
                                    status.text = getString(R.string.voice_note_failed)
                                    button.isEnabled = true
                                    button.text = getString(R.string.voice_note_start)
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
        dialog.setOnDismissListener {
            recorder.stop()
            patientVoiceNoteRecorder = null
        }
        dialog.show()
    }

    private fun currentAppLanguage(): String =
        AppCompatDelegate.getApplicationLocales().get(0)?.language ?: "en"

    private fun chooseLanguage() {
        val current = currentAppLanguage()
        AlertDialog.Builder(this)
            .setTitle(getString(R.string.language_title))
            .setSingleChoiceItems(arrayOf("English", "हिन्दी"), if (current == "hi") 1 else 0) { dialog, which ->
                val tag = if (which == 1) "hi" else "en"
                AppCompatDelegate.setApplicationLocales(LocaleListCompat.forLanguageTags(tag))
                dialog.dismiss()
            }
            .show()
    }

    private fun startVoiceNotePolling() {
        voiceNotePollJob?.cancel()
        voiceNotePollJob = lifecycleScope.launch {
            while (isActive) {
                fetchLatestNurseVoiceNote()
                delay(3000)
            }
        }
    }

    private suspend fun fetchLatestNurseVoiceNote() {
        val room = UserSession.getRoomNumber(this) ?: return
        val jsonText = ServerUploader(this).getVoiceNotes(room) ?: return
        try {
            val arr = org.json.JSONArray(jsonText)
            for (i in 0 until arr.length()) {
                val item = arr.getJSONObject(i)
                if (item.optString("sender_role") == "nurse") {
                    val id = item.optInt("id")
                    val name = item.optString("sender_name", "Nurse")
                    val duration = item.optInt("duration_ms", 0)
                    val translatedText = item.optString("translated_text", "")
                    val originalText = item.optString("original_text", "")
                    val targetLang = item.optString("target_language", "en")
                    withContext(Dispatchers.Main) {
                        binding.cardVoiceNote.visibility = View.VISIBLE
                        binding.tvVoiceNoteInfo.text =
                            if (translatedText.isNotEmpty()) "$translatedText\n(Original: $originalText)" else "$name sent a voice note"
                        
                        latestNurseVoiceNoteText = translatedText.ifBlank { "Voice note from $name." }
                        latestNurseVoiceNoteLang = targetLang
                        latestNurseVoiceNoteOrig = originalText
                        
                        if (latestNurseVoiceNoteId == null) {
                            latestNurseVoiceNoteId = id
                        } else if (latestNurseVoiceNoteId != id) {
                            latestNurseVoiceNoteId = id
                            Snackbar.make(binding.root, "New voice message from the nurse", Snackbar.LENGTH_LONG).show()
                        }
                    }
                    return
                }
            }
        } catch (_: Exception) {
            // Ignore malformed metadata; next poll will retry.
        }
    }

    private fun playLatestNurseVoiceNote() {
        val id = latestNurseVoiceNoteId ?: return
        val text = latestNurseVoiceNoteText ?: return
        val lang = latestNurseVoiceNoteLang ?: "en"
        
        binding.btnPlayVoiceNote.text = "⏹ Playing…"
        ttsManager.speakVoiceNote(text, lang, id)
        
        // Reset the button after a short delay or depend on user to press play again
        // Actually, TtsManager handles completion callbacks, but we don't have a direct hook here.
        // For now, reset it after a generic delay.
        lifecycleScope.launch {
            kotlinx.coroutines.delay(5000)
            binding.btnPlayVoiceNote.text = "▶ Play voice message"
        }
    }

    override fun onBackPressed() {
        if (binding.drawerLayout.isDrawerOpen(GravityCompat.START)) {
            binding.drawerLayout.closeDrawer(GravityCompat.START)
        } else {
            super.onBackPressed()
        }
    }

    // ── Drawer ────────────────────────────────────────────────────────────────

    private fun setupDrawer() {
        binding.navView.setNavigationItemSelectedListener(this)

        binding.btnMenu.setOnClickListener {
            if (binding.drawerLayout.isDrawerOpen(GravityCompat.START)) {
                binding.drawerLayout.closeDrawer(GravityCompat.START)
            } else {
                binding.drawerLayout.openDrawer(GravityCompat.START)
            }
        }
    }

    private fun populateDrawerHeader() {
        val header     = binding.navView.getHeaderView(0)
        val fullName   = UserSession.getFullName(this) ?: "Patient"
        val roomNumber = UserSession.getRoomNumber(this)

        // Avatar initials (up to 2 words)
        val initials = fullName.split(" ")
            .filter { it.isNotEmpty() }
            .take(2)
            .joinToString("") { it.first().uppercaseChar().toString() }
        header.findViewById<android.widget.TextView>(R.id.tvAvatarInitials).text = initials

        header.findViewById<android.widget.TextView>(R.id.tvNavName).text =
            fullName
        header.findViewById<android.widget.TextView>(R.id.tvNavRole).text =
            getString(R.string.drawer_role_patient)

        // Room row
        header.findViewById<android.widget.TextView>(R.id.tvNavRoom).text =
            if (!roomNumber.isNullOrEmpty()) getString(R.string.drawer_room_fmt, roomNumber)
            else getString(R.string.drawer_room_unknown)

        // Status row — reflects current UiState
        header.findViewById<android.widget.TextView>(R.id.tvNavStatus).text =
            getString(R.string.drawer_status_active)
    }

    /** Keep the sidebar status line in sync with the listening state. */
    private fun updateDrawerStatus(listening: Boolean) {
        val header = binding.navView.getHeaderView(0)
        header.findViewById<android.widget.TextView>(R.id.tvNavStatus).text =
            if (listening) getString(R.string.drawer_status_listening)
            else getString(R.string.drawer_status_wake_word)
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

    // ── Profile dialog (editable) ─────────────────────────────────────────────

    private fun showProfileDialog() {
        val view = layoutInflater.inflate(R.layout.dialog_edit_profile_patient, null)

        // Populate read-only info
        view.findViewById<TextView>(R.id.tvProfileUsername).text =
            "Logged in as: ${UserSession.getUsername(this) ?: "—"}"

        // Pre-fill editable fields
        val etName = view.findViewById<com.google.android.material.textfield.TextInputEditText>(R.id.etFullName)
        val etRoom = view.findViewById<com.google.android.material.textfield.TextInputEditText>(R.id.etRoomNumber)
        etName.setText(UserSession.getFullName(this) ?: "")
        etRoom.setText(UserSession.getRoomNumber(this) ?: "")

        AlertDialog.Builder(this)
            .setTitle("Edit Profile")
            .setView(view)
            .setPositiveButton("Save") { _, _ ->
                val newName = etName.text.toString().trim()
                val newRoom = etRoom.text.toString().trim()
                if (newName.isBlank()) {
                    Toast.makeText(this, "Name cannot be empty", Toast.LENGTH_SHORT).show()
                    return@setPositiveButton
                }
                savePatientProfile(newName, newRoom.ifBlank { null })
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun savePatientProfile(fullName: String, roomNumber: String?) {
        val prefs     = getSharedPreferences(ServerUploader.PREFS_NAME, Context.MODE_PRIVATE)
        val serverUrl = prefs.getString(ServerUploader.KEY_SERVER_URL, ServerUploader.DEFAULT_SERVER_URL)
            ?: ServerUploader.DEFAULT_SERVER_URL
        val token     = UserSession.getToken(this) ?: return

        val body = JSONObject().apply {
            put("full_name", fullName)
            if (roomNumber != null) put("room_number", roomNumber)
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
                        val json = JSONObject(respBody)
                        val updatedName = json.optString("full_name").ifEmpty { fullName }
                        val updatedRoom = json.optString("room_number").ifEmpty { roomNumber }

                        // Persist into session
                        UserSession.save(
                            context    = this@MainActivity,
                            token      = token,
                            userId     = UserSession.getUserId(this@MainActivity),
                            fullName   = updatedName,
                            role       = UserSession.getRole(this@MainActivity) ?: "patient",
                            roomNumber = updatedRoom,
                            username   = UserSession.getUsername(this@MainActivity)
                        )

                        // Refresh drawer + title
                        populateDrawerHeader()
                        binding.tvAppTitle.text = "CareVoice AI | $updatedName (Room ${updatedRoom ?: "—"})"

                        // Also update ServerUploader room pref
                        if (!updatedRoom.isNullOrEmpty()) {
                            getSharedPreferences(ServerUploader.PREFS_NAME, Context.MODE_PRIVATE)
                                .edit().putString(ServerUploader.KEY_ROOM_ID, updatedRoom).apply()
                        }

                        Toast.makeText(this@MainActivity, "Profile updated", Toast.LENGTH_SHORT).show()
                    } else {
                        Toast.makeText(this@MainActivity, "Update failed (${resp.code})", Toast.LENGTH_SHORT).show()
                    }
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    Toast.makeText(this@MainActivity, "Network error: ${e.message}", Toast.LENGTH_SHORT).show()
                }
            }
        }
    }

    // ── Settings ──────────────────────────────────────────────────────────────

    private fun showSettingsDialog() {
        val prefs      = getSharedPreferences(ServerUploader.PREFS_NAME, Context.MODE_PRIVATE)
        val currentUrl = prefs.getString(ServerUploader.KEY_SERVER_URL, ServerUploader.DEFAULT_SERVER_URL)

        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            val pad = (16 * resources.displayMetrics.density).toInt()
            setPadding(pad, pad, pad, pad)
        }
        val etServerUrl = EditText(this).apply {
            hint      = getString(R.string.settings_hint_url)
            inputType = android.text.InputType.TYPE_CLASS_TEXT or
                        android.text.InputType.TYPE_TEXT_VARIATION_URI
            setText(currentUrl)
        }
        layout.addView(etServerUrl)

        AlertDialog.Builder(this)
            .setTitle(getString(R.string.settings_title))
            .setView(layout)
            .setPositiveButton(getString(R.string.settings_save)) { _, _ ->
                val newUrl = etServerUrl.text.toString().trim().trimEnd('/')
                if (newUrl.isNotBlank()) {
                    prefs.edit().putString(ServerUploader.KEY_SERVER_URL, newUrl).apply()
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

    // ── Buttons ───────────────────────────────────────────────────────────────

    private fun setupButtons() {
        // Call Nurse = instant manual alert, no recording needed
        binding.btnCallNurse.setOnClickListener { viewModel.callNurseNow() }
        binding.btnStop.setOnClickListener { viewModel.stopListening() }
    }

    // ── ViewModel observation ─────────────────────────────────────────────────

    private fun observeViewModel() {
        viewModel.uiState.observe(this)  { renderState(it) }
        viewModel.rmsLevel.observe(this) { animateAmplitudeBar(it) }
    }

    // ── State rendering ───────────────────────────────────────────────────────

    private fun renderState(state: MainViewModel.UiState) {
        when (state) {
            is MainViewModel.UiState.Idle -> {
                binding.tvStatus.text          = getString(R.string.status_idle)
                binding.btnCallNurse.isEnabled = true
                binding.btnStop.isEnabled      = false
                setDownloadUiVisible(false)
            }
            is MainViewModel.UiState.Downloading -> {
                binding.tvStatus.text          = getString(R.string.status_preparing)
                binding.btnCallNurse.isEnabled = true
                binding.btnStop.isEnabled      = false
                setDownloadUiVisible(true)
                binding.tvDownloadPercent.text =
                    getString(R.string.download_progress_fmt, state.percent)
                binding.progressBarDownload.progress = state.percent
            }
            is MainViewModel.UiState.ModelReady -> {
                binding.tvStatus.text          = getString(R.string.status_ready)
                binding.btnCallNurse.isEnabled = true
                binding.btnStop.isEnabled      = false
                setDownloadUiVisible(false)
                updateDrawerStatus(false)
                checkMicPermissionAndStartListening()
            }
            is MainViewModel.UiState.WakeWordListening -> {
                binding.tvStatus.text          = getString(R.string.patient_wake_listening)
                binding.btnCallNurse.isEnabled = true
                binding.btnStop.isEnabled      = false
                setDownloadUiVisible(false)
                updateDrawerStatus(false)
            }
            is MainViewModel.UiState.Listening -> {
                // Energy-VAD fallback path
                binding.tvStatus.text          = getString(R.string.patient_listening)
                binding.btnCallNurse.isEnabled = true
                binding.btnStop.isEnabled      = false
                setDownloadUiVisible(false)
                updateDrawerStatus(true)
            }
            is MainViewModel.UiState.Triggered -> {
                binding.tvStatus.text          = getString(R.string.patient_recording)
                binding.btnCallNurse.isEnabled = true
                binding.btnStop.isEnabled      = true
                updateDrawerStatus(true)
            }
            is MainViewModel.UiState.ManualRecording -> {
                binding.tvStatus.text          = getString(R.string.patient_recording)
                binding.btnCallNurse.isEnabled = false
                binding.btnStop.isEnabled      = true
                updateDrawerStatus(true)
            }
            is MainViewModel.UiState.SpeechDetected -> {
                binding.tvStatus.text          = getString(R.string.patient_sending)
                binding.btnCallNurse.isEnabled = true
                binding.btnStop.isEnabled      = false
                updateDrawerStatus(true)
            }
            is MainViewModel.UiState.Sent -> {
                binding.tvStatus.text          = getString(R.string.patient_notified)
                binding.btnCallNurse.isEnabled = true
                binding.btnStop.isEnabled      = false
                updateDrawerStatus(false)
            }
            is MainViewModel.UiState.Ignored -> {
                binding.tvStatus.text          = getString(R.string.patient_wake_listening)
                binding.btnCallNurse.isEnabled = true
                binding.btnStop.isEnabled      = false
                updateDrawerStatus(false)
            }
            is MainViewModel.UiState.DownloadError -> {
                binding.tvStatus.text          = getString(R.string.status_error)
                binding.btnCallNurse.isEnabled = true
                binding.btnStop.isEnabled      = false
                setDownloadUiVisible(false)
                showDownloadErrorSnackbar()
            }
            is MainViewModel.UiState.UploadError -> {
                binding.tvStatus.text          = getString(R.string.patient_call_error)
                binding.btnCallNurse.isEnabled = true
                binding.btnStop.isEnabled      = false
                Toast.makeText(this, "Upload failed — will retry", Toast.LENGTH_SHORT).show()
            }
        }
    }

    private fun setDownloadUiVisible(visible: Boolean) {
        val v = if (visible) View.VISIBLE else View.GONE
        binding.progressBarDownload.visibility = v
        binding.tvDownloadPercent.visibility   = v
    }

    // ── Amplitude bar ─────────────────────────────────────────────────────────

    private fun animateAmplitudeBar(rms: Float) {
        val track = binding.amplitudeTrack
        val bar   = binding.viewAmplitudeBar
        if (track.width == 0) return
        val targetWidth = (track.width * rms.coerceIn(0f, 1f)).toInt()
        bar.animate().setDuration(80).withEndAction {
            val p = bar.layoutParams; p.width = targetWidth; bar.layoutParams = p
        }.start()
    }

    // ── Permission handling ───────────────────────────────────────────────────

    private fun checkMicPermissionAndStartListening() {
        when {
            ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
                    == PackageManager.PERMISSION_GRANTED -> viewModel.startListening()
            shouldShowRequestPermissionRationale(Manifest.permission.RECORD_AUDIO) -> {
                AlertDialog.Builder(this)
                    .setTitle(getString(R.string.perm_title))
                    .setMessage(getString(R.string.perm_message))
                    .setPositiveButton(getString(R.string.perm_allow)) { _, _ ->
                        requestMicPermission.launch(Manifest.permission.RECORD_AUDIO)
                    }
                    .setNegativeButton(getString(R.string.settings_cancel), null)
                    .show()
            }
            else -> requestMicPermission.launch(Manifest.permission.RECORD_AUDIO)
        }
    }

    // ── Snackbars ─────────────────────────────────────────────────────────────

    private fun showDownloadErrorSnackbar() {
        Snackbar.make(binding.root, getString(R.string.snack_download_failed), Snackbar.LENGTH_INDEFINITE)
            .setAction(getString(R.string.snack_retry)) { viewModel.ensureModelReady() }.show()
    }
}
