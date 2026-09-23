package com.carevoice.app

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
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
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.lifecycleScope
import com.carevoice.app.databinding.ActivityMainBinding
import com.google.android.material.navigation.NavigationView
import com.google.android.material.snackbar.Snackbar
import kotlinx.coroutines.Dispatchers
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

    // ── Lifecycle ─────────────────────────────────────────────────────────────

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding   = ActivityMainBinding.inflate(layoutInflater)
        viewModel = ViewModelProvider(this)[MainViewModel::class.java]
        setContentView(binding.root)

        val patientName = UserSession.getFullName(this) ?: "Patient"
        val roomNumber  = UserSession.getRoomNumber(this) ?: "—"
        binding.tvAppTitle.text = "CareVoice AI | $patientName (Room $roomNumber)"

        setupDrawer()
        populateDrawerHeader()
        setupButtons()
        observeViewModel()
        viewModel.ensureModelReady()

        // Connect the call-signaling socket so the patient can receive incoming
        // calls from their nurse (and place outgoing ones).
        ensureCallSignaling()
    }

    override fun onResume() {
        super.onResume()
        // Reconnect signaling if it dropped while backgrounded.
        ensureCallSignaling()
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
        // Voice Call = real-time WebRTC call to the patient's attending nurse.
        binding.btnVoiceCall.setOnClickListener { startVoiceCall() }
    }

    // ── Real-time voice call ────────────────────────────────────────────────---

    /**
     * Ensure the signaling socket is connected. Called on resume so an incoming
     * call can reach this patient even when they aren't actively in the app.
     */
    private fun ensureCallSignaling() {
        val serverUrl = getSharedPreferences(ServerUploader.PREFS_NAME, Context.MODE_PRIVATE)
            .getString(ServerUploader.KEY_SERVER_URL, ServerUploader.DEFAULT_SERVER_URL)
            ?: ServerUploader.DEFAULT_SERVER_URL
        val token = UserSession.getToken(this) ?: return
        CallSession.ensureSignaling(this, serverUrl, token)
    }

    /** Place a real-time voice call to the attending nurse (server-routed). */
    private fun startVoiceCall() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED) {
            requestMicPermission.launch(Manifest.permission.RECORD_AUDIO)
            Toast.makeText(this, getString(R.string.snack_mic_denied), Toast.LENGTH_SHORT).show()
            return
        }
        ensureCallSignaling()
        // to=null → server routes to this patient's attending nurse.
        CallSession.placeCall(to = null, displayName = "Nurse", roomId = UserSession.getRoomNumber(this))
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
                binding.tvStatus.text          = "🎙 Say \"Help\" to call the nurse"
                binding.btnCallNurse.isEnabled = true
                binding.btnStop.isEnabled      = false
                setDownloadUiVisible(false)
                updateDrawerStatus(false)
            }
            is MainViewModel.UiState.Listening -> {
                // Energy-VAD fallback path
                binding.tvStatus.text          = "👂 Listening..."
                binding.btnCallNurse.isEnabled = true
                binding.btnStop.isEnabled      = false
                setDownloadUiVisible(false)
                updateDrawerStatus(true)
            }
            is MainViewModel.UiState.Triggered -> {
                binding.tvStatus.text          = "🔴 Recording your message..."
                binding.btnCallNurse.isEnabled = true
                binding.btnStop.isEnabled      = true
                updateDrawerStatus(true)
            }
            is MainViewModel.UiState.ManualRecording -> {
                binding.tvStatus.text          = "🔴 Recording... speak your message"
                binding.btnCallNurse.isEnabled = false
                binding.btnStop.isEnabled      = true
                updateDrawerStatus(true)
            }
            is MainViewModel.UiState.SpeechDetected -> {
                binding.tvStatus.text          = "📤 Sending..."
                binding.btnCallNurse.isEnabled = true
                binding.btnStop.isEnabled      = false
                updateDrawerStatus(true)
            }
            is MainViewModel.UiState.Sent -> {
                binding.tvStatus.text          = "✓ Nurse has been notified"
                binding.btnCallNurse.isEnabled = true
                binding.btnStop.isEnabled      = false
                updateDrawerStatus(false)
            }
            is MainViewModel.UiState.Ignored -> {
                binding.tvStatus.text          = "🎙 Say \"Help\" to call the nurse"
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
                binding.tvStatus.text          = "⚠ Could not reach server"
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
