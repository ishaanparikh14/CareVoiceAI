package com.carevoice.app

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.view.View
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.carevoice.app.databinding.ActivityLoginBinding
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.FormBody
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject

class LoginActivity : AppCompatActivity() {

    private lateinit var binding: ActivityLoginBinding
    private val client = OkHttpClient()

    // Tracks which role the user has selected in the chip group
    private var selectedRole: Role = Role.PATIENT

    private enum class Role { PATIENT, NURSE }

    // ── Colors resolved once from resources ───────────────────────────────────
    private val patientAccent by lazy { getColor(R.color.colorLoginPatientAccent) }
    private val nurseAccent   by lazy { getColor(R.color.colorLoginNurseAccent) }
    // ── Lifecycle ─────────────────────────────────────────────────────────────

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // If a valid session already exists, skip straight to the right screen
        if (UserSession.isLoggedIn(this)) {
            navigateByRole(UserSession.getRole(this))
            return
        }

        binding = ActivityLoginBinding.inflate(layoutInflater)
        setContentView(binding.root)

        // Pre-fill the server URL field from saved prefs
        val prefs = getSharedPreferences(ServerUploader.PREFS_NAME, Context.MODE_PRIVATE)
        val savedUrl = prefs.getString(ServerUploader.KEY_SERVER_URL, ServerUploader.DEFAULT_SERVER_URL)
            ?: ServerUploader.DEFAULT_SERVER_URL
        binding.etServerUrl.setText(savedUrl)

        setupRoleToggle()

        binding.btnLogin.setOnClickListener {
            val username = binding.etUsername.text.toString().trim()
            val password = binding.etPassword.text.toString()
            if (username.isEmpty() || password.isEmpty()) {
                Toast.makeText(this, "Please enter credentials", Toast.LENGTH_SHORT).show()
                return@setOnClickListener
            }
            performLogin(username, password)
        }

        binding.tvRegister.setOnClickListener {
            RegisterActivity.startForRole(
                this,
                if (selectedRole == Role.NURSE) "nurse" else "patient"
            )
        }
    }

    // ── Role toggle ───────────────────────────────────────────────────────────

    private fun setupRoleToggle() {
        binding.chipPatient.setOnCheckedChangeListener { _, checked ->
            if (checked) applyRole(Role.PATIENT)
        }
        binding.chipNurse.setOnCheckedChangeListener { _, checked ->
            if (checked) applyRole(Role.NURSE)
        }
        applyRole(Role.PATIENT)
    }

    private fun applyRole(role: Role) {
        selectedRole = role

        val isNurse = role == Role.NURSE
        val targetColor = if (isNurse) nurseAccent else patientAccent

        // Swap gradient drawable on header
        binding.viewHeaderBg.setBackgroundResource(
            if (isNurse) R.drawable.bg_login_header_nurse
            else R.drawable.bg_login_header_patient
        )

        // Tint the login button to match
        binding.btnLogin.backgroundTintList =
            android.content.res.ColorStateList.valueOf(targetColor)

        // Update title and subtitle
        binding.tvLoginTitle.text = getString(
            if (isNurse) R.string.login_title_nurse else R.string.login_title_patient
        )
        binding.tvLoginSub.text = getString(
            if (isNurse) R.string.login_subtitle_nurse else R.string.login_subtitle_patient
        )
        binding.tvRoleLabel.text = if (isNurse) "Sign in as Nurse" else "Sign in as Patient"

        // Show server URL field only for nurses (patients use the saved URL silently)
        binding.tilServerUrl.visibility = if (isNurse) View.VISIBLE else View.GONE

        // Register link color tracks the active role accent
        binding.tvRegister.setTextColor(targetColor)

        // Clear any previous field errors
        binding.tilUsername.error = null
        binding.tilPassword.error = null
    }

    // ── Auth ──────────────────────────────────────────────────────────────────

    private fun performLogin(username: String, password: String) {
        binding.pbLogin.visibility = View.VISIBLE
        binding.btnLogin.isEnabled = false

        val prefs = getSharedPreferences(ServerUploader.PREFS_NAME, Context.MODE_PRIVATE)

        // Nurses can override the server URL directly on the login screen
        val serverUrl: String = if (selectedRole == Role.NURSE) {
            val typed = binding.etServerUrl.text.toString().trim().trimEnd('/')
            if (typed.isNotEmpty()) {
                prefs.edit().putString(ServerUploader.KEY_SERVER_URL, typed).apply()
                typed
            } else {
                prefs.getString(ServerUploader.KEY_SERVER_URL, ServerUploader.DEFAULT_SERVER_URL)
                    ?: ServerUploader.DEFAULT_SERVER_URL
            }
        } else {
            prefs.getString(ServerUploader.KEY_SERVER_URL, ServerUploader.DEFAULT_SERVER_URL)
                ?: ServerUploader.DEFAULT_SERVER_URL
        }

        lifecycleScope.launch(Dispatchers.IO) {
            try {
                val formBody = FormBody.Builder()
                    .add("username", username)
                    .add("password", password)
                    .build()

                val request = Request.Builder()
                    .url("$serverUrl/auth/login")
                    .post(formBody)
                    .build()

                val response = client.newCall(request).execute()
                val body     = response.body?.string()

                withContext(Dispatchers.Main) {
                    binding.pbLogin.visibility = View.GONE
                    binding.btnLogin.isEnabled = true

                    if (response.isSuccessful && body != null) {
                        val json     = JSONObject(body)
                        val roleStr  = json.getString("role")

                        // Hard role enforcement — nurse credentials cannot log in as patient and vice versa
                        val expectedRole = if (selectedRole == Role.NURSE) "nurse" else "patient"
                        if (roleStr != expectedRole) {
                            val msg = if (selectedRole == Role.NURSE)
                                "These are patient credentials. Switch to Patient to continue."
                            else
                                "These are nurse credentials. Switch to Nurse to continue."
                            Toast.makeText(this@LoginActivity, msg, Toast.LENGTH_LONG).show()
                            return@withContext
                        }

                        // Persist session
                        UserSession.save(
                            context    = this@LoginActivity,
                            token      = json.getString("access_token"),
                            userId     = json.getInt("user_id"),
                            fullName   = json.getString("full_name"),
                            role       = roleStr,
                            roomNumber = json.optString("room_number").ifEmpty { null },
                            ward       = json.optString("ward").ifEmpty { null },
                            username   = username
                        )

                        // Store room ID for ServerUploader (patient only)
                        val room = json.optString("room_number")
                        if (room.isNotEmpty()) {
                            prefs.edit().putString(ServerUploader.KEY_ROOM_ID, room).apply()
                        }

                        // Store ward in ServerUploader prefs for backwards compatibility
                        val ward = json.optString("ward")
                        if (ward.isNotEmpty()) {
                            prefs.edit().putString("ward", ward).apply()
                        }

                        navigateByRole(roleStr)

                    } else {
                        val msg = when (response.code) {
                            401  -> "Invalid username or password"
                            else -> "Server error (${response.code})"
                        }
                        Toast.makeText(this@LoginActivity, msg, Toast.LENGTH_SHORT).show()
                    }
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    binding.pbLogin.visibility = View.GONE
                    binding.btnLogin.isEnabled = true
                    Toast.makeText(
                        this@LoginActivity,
                        "Connection failed: ${e.message}",
                        Toast.LENGTH_LONG
                    ).show()
                }
            }
        }
    }

    // ── Navigation ────────────────────────────────────────────────────────────

    private fun navigateByRole(role: String?) {
        val dest = if (role == "nurse") NurseActivity::class.java else MainActivity::class.java
        startActivity(Intent(this, dest).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK
        })
    }
}
