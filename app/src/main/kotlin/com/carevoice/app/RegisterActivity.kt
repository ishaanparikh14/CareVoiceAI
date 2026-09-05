package com.carevoice.app

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.view.View
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.carevoice.app.databinding.ActivityRegisterBinding
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * RegisterActivity — create a new patient or nurse account.
 *
 * Role is passed in via Intent extra EXTRA_ROLE ("patient" | "nurse").
 * The layout shows/hides role-specific fields accordingly.
 *
 * Patient fields  : username, password, full_name, room, age*, diagnosis*, attending*
 * Nurse fields    : username, password, full_name, ward, admin_key
 * (* optional)
 *
 * On success: shows a toast and finishes back to LoginActivity.
 */
class RegisterActivity : AppCompatActivity() {

    companion object {
        const val EXTRA_ROLE = "role"

        fun startForRole(context: Context, role: String) {
            context.startActivity(
                Intent(context, RegisterActivity::class.java)
                    .putExtra(EXTRA_ROLE, role)
            )
        }
    }

    private lateinit var binding: ActivityRegisterBinding
    private val http = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .build()

    private var isNurse = false

    // ── Lifecycle ─────────────────────────────────────────────────────────────

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityRegisterBinding.inflate(layoutInflater)
        setContentView(binding.root)

        isNurse = intent.getStringExtra(EXTRA_ROLE) == "nurse"

        // Pre-fill server URL from saved prefs
        val prefs = getSharedPreferences(ServerUploader.PREFS_NAME, Context.MODE_PRIVATE)
        val savedUrl = prefs.getString(ServerUploader.KEY_SERVER_URL, ServerUploader.DEFAULT_SERVER_URL)
            ?: ServerUploader.DEFAULT_SERVER_URL
        binding.etServerUrl.setText(savedUrl)

        applyRole()
        setupButtons()
    }

    // ── Role UI ───────────────────────────────────────────────────────────────

    private fun applyRole() {
        if (isNurse) {
            binding.viewHeaderBg.setBackgroundResource(R.drawable.bg_login_header_nurse)
            binding.tvRegisterTitle.text = getString(R.string.register_title_nurse)
            binding.tvRegisterSub.text   = getString(R.string.register_subtitle_nurse)
            binding.btnRegister.backgroundTintList =
                android.content.res.ColorStateList.valueOf(getColor(R.color.colorLoginNurseAccent))
            binding.tvSignIn.setTextColor(getColor(R.color.colorLoginNurseAccent))

            // Show nurse fields, hide patient fields
            binding.tilWard.visibility      = View.VISIBLE
            binding.tilAdminKey.visibility  = View.VISIBLE
            binding.tilRoom.visibility      = View.GONE
            binding.tilAge.visibility       = View.GONE
            binding.tilDiagnosis.visibility = View.GONE
            binding.tilAttending.visibility = View.GONE
        } else {
            binding.viewHeaderBg.setBackgroundResource(R.drawable.bg_login_header_patient)
            binding.tvRegisterTitle.text = getString(R.string.register_title_patient)
            binding.tvRegisterSub.text   = getString(R.string.register_subtitle_patient)
            binding.btnRegister.backgroundTintList =
                android.content.res.ColorStateList.valueOf(getColor(R.color.colorLoginPatientAccent))
            binding.tvSignIn.setTextColor(getColor(R.color.colorLoginPatientAccent))

            // Show patient fields, hide nurse fields
            binding.tilRoom.visibility      = View.VISIBLE
            binding.tilAge.visibility       = View.VISIBLE
            binding.tilDiagnosis.visibility = View.VISIBLE
            binding.tilAttending.visibility = View.VISIBLE
            binding.tilWard.visibility      = View.GONE
            binding.tilAdminKey.visibility  = View.GONE
        }
    }

    // ── Buttons ───────────────────────────────────────────────────────────────

    private fun setupButtons() {
        binding.btnBack.setOnClickListener { finish() }
        binding.tvSignIn.setOnClickListener { finish() }
        binding.btnRegister.setOnClickListener { attemptRegister() }
    }

    // ── Validation + submit ───────────────────────────────────────────────────

    private fun attemptRegister() {
        val username  = binding.etUsername.text.toString().trim()
        val password  = binding.etPassword.text.toString()
        val fullName  = binding.etFullName.text.toString().trim()
        val serverUrl = binding.etServerUrl.text.toString().trim().trimEnd('/')

        // Clear previous errors
        binding.tilUsername.error  = null
        binding.tilPassword.error  = null
        binding.tilFullName.error  = null
        binding.tilServerUrl.error = null

        var valid = true

        if (username.isEmpty()) {
            binding.tilUsername.error = "Required"; valid = false
        }
        if (password.length < 6) {
            binding.tilPassword.error = "Min 6 characters"; valid = false
        }
        if (fullName.isEmpty()) {
            binding.tilFullName.error = "Required"; valid = false
        }
        if (serverUrl.isEmpty()) {
            binding.tilServerUrl.error = "Required"; valid = false
        }

        if (isNurse) {
            val ward     = binding.etWard.text.toString().trim()
            val adminKey = binding.etAdminKey.text.toString()
            if (ward.isEmpty()) {
                binding.tilWard.error = "Required"; valid = false
            }
            if (adminKey.isEmpty()) {
                binding.tilAdminKey.error = "Required"; valid = false
            }
        } else {
            val room = binding.etRoom.text.toString().trim()
            if (room.isEmpty()) {
                binding.tilRoom.error = "Required"; valid = false
            }
        }

        if (!valid) return

        // Save server URL for future use
        getSharedPreferences(ServerUploader.PREFS_NAME, Context.MODE_PRIVATE)
            .edit().putString(ServerUploader.KEY_SERVER_URL, serverUrl).apply()

        submitRegistration(username, password, fullName, serverUrl)
    }

    private fun submitRegistration(
        username:  String,
        password:  String,
        fullName:  String,
        serverUrl: String,
    ) {
        binding.pbRegister.visibility  = View.VISIBLE
        binding.btnRegister.isEnabled  = false

        lifecycleScope.launch(Dispatchers.IO) {
            try {
                val body: JSONObject
                val endpoint: String

                if (isNurse) {
                    endpoint = "$serverUrl/auth/register/nurse"
                    body = JSONObject().apply {
                        put("username",  username)
                        put("password",  password)
                        put("full_name", fullName)
                        put("ward",      binding.etWard.text.toString().trim())
                        put("admin_key", binding.etAdminKey.text.toString())
                    }
                } else {
                    endpoint = "$serverUrl/auth/register/patient"
                    body = JSONObject().apply {
                        put("username",    username)
                        put("password",    password)
                        put("full_name",   fullName)
                        put("room_number", binding.etRoom.text.toString().trim())
                        val ageStr = binding.etAge.text.toString().trim()
                        if (ageStr.isNotEmpty()) put("age", ageStr.toInt())
                        val diagnosis = binding.etDiagnosis.text.toString().trim()
                        if (diagnosis.isNotEmpty()) put("diagnosis", diagnosis)
                        val attending = binding.etAttending.text.toString().trim()
                        if (attending.isNotEmpty()) put("attending", attending)
                    }
                }

                val reqBody = body.toString()
                    .toRequestBody("application/json".toMediaType())

                val request = Request.Builder()
                    .url(endpoint)
                    .post(reqBody)
                    .build()

                val response = http.newCall(request).execute()
                val respBody = response.body?.string()

                withContext(Dispatchers.Main) {
                    binding.pbRegister.visibility = View.GONE
                    binding.btnRegister.isEnabled = true

                    when (response.code) {
                        201 -> {
                            Toast.makeText(
                                this@RegisterActivity,
                                getString(R.string.register_success),
                                Toast.LENGTH_LONG
                            ).show()
                            finish()
                        }
                        409 -> {
                            binding.tilUsername.error =
                                getString(R.string.register_err_conflict)
                        }
                        403 -> {
                            binding.tilAdminKey.error =
                                getString(R.string.register_err_admin_key)
                        }
                        422 -> {
                            // Pydantic validation error — parse detail if possible
                            val detail = runCatching {
                                JSONObject(respBody ?: "")
                                    .getJSONArray("detail")
                                    .getJSONObject(0)
                                    .getString("msg")
                            }.getOrDefault("Please check your inputs.")
                            Toast.makeText(this@RegisterActivity, detail, Toast.LENGTH_LONG).show()
                        }
                        else -> {
                            Toast.makeText(
                                this@RegisterActivity,
                                "Server error (${response.code})",
                                Toast.LENGTH_SHORT
                            ).show()
                        }
                    }
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    binding.pbRegister.visibility = View.GONE
                    binding.btnRegister.isEnabled = true
                    Toast.makeText(
                        this@RegisterActivity,
                        "Connection failed: ${e.message}",
                        Toast.LENGTH_LONG
                    ).show()
                }
            }
        }
    }
}
