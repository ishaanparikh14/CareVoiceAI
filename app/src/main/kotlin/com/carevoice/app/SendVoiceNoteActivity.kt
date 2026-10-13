package com.carevoice.app

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.media.MediaPlayer
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.MenuItem
import android.view.View
import android.widget.ArrayAdapter
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.carevoice.app.databinding.ActivityVoiceNoteSendBinding
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

/**
 * Record and send a voice note in the sender's own voice.
 *
 * Patient: always sends to their own room (their nurse).
 * Nurse: picks one of their assigned rooms (pre-selected when replying).
 * Flow: record → play back → send or discard.
 */
class SendVoiceNoteActivity : AppCompatActivity() {

    companion object {
        private const val EXTRA_ROOM = "room"
        private const val EXTRA_ALERT_ID = "alert_id"
        private const val MAX_MS = 30_000L
        private const val PREF_LANG = "voice_note_lang"

        fun open(context: Context, room: String? = null, alertId: Int? = null) {
            context.startActivity(
                Intent(context, SendVoiceNoteActivity::class.java).apply {
                    room?.let { putExtra(EXTRA_ROOM, it) }
                    alertId?.let { putExtra(EXTRA_ALERT_ID, it) }
                }
            )
        }
    }

    private lateinit var binding: ActivityVoiceNoteSendBinding
    private lateinit var uploader: ServerUploader
    private lateinit var recorder: VoiceNoteRecorder

    private val isNurse by lazy { UserSession.getRole(this) == "nurse" }
    private var rooms: List<PatientRoom> = emptyList()
    private var pendingWav: ByteArray? = null
    private var pendingDurationMs = 0
    private var recordStartedAt = 0L
    private var player: MediaPlayer? = null
    private var playerFile: File? = null

    private val handler = Handler(Looper.getMainLooper())
    private val ticker = object : Runnable {
        override fun run() {
            if (!recorder.isRecording()) return
            showTimer(System.currentTimeMillis() - recordStartedAt)
            handler.postDelayed(this, 250)
        }
    }

    private val micPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (granted) startRecording()
            else Toast.makeText(this, R.string.vn_mic_denied, Toast.LENGTH_LONG).show()
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityVoiceNoteSendBinding.inflate(layoutInflater)
        setContentView(binding.root)
        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        title = getString(R.string.vn_send)
        if (isNurse) {
            supportActionBar?.setBackgroundDrawable(
                android.graphics.drawable.ColorDrawable(getColor(R.color.colorLoginNurseAccent))
            )
            window.statusBarColor = getColor(R.color.cv_nurse_dark)
        }
        binding.fabRecord.backgroundTintList = ColorStateList.valueOf(accentColor())

        uploader = ServerUploader(this)
        recorder = VoiceNoteRecorder(this).apply { setOnFinished(::onRecordingFinished) }

        setupRecipient()
        setupLanguage()

        binding.fabRecord.setOnClickListener { toggleRecording() }
        binding.btnPreview.setOnClickListener { togglePreview() }
        binding.btnDiscard.setOnClickListener { discard() }
        binding.btnSend.setOnClickListener { send() }
        showTimer(0)
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        if (item.itemId == android.R.id.home) { finish(); return true }
        return super.onOptionsItemSelected(item)
    }

    override fun onStop() {
        super.onStop()
        if (recorder.isRecording()) recorder.stop()
        stopPreview()
    }

    override fun onDestroy() {
        super.onDestroy()
        handler.removeCallbacksAndMessages(null)
    }

    /** Teal for nurses, indigo for patients. */
    private fun accentColor(): Int =
        getColor(if (isNurse) R.color.colorLoginNurseAccent else R.color.colorLoginPatientAccent)

    // ── Recipient + language ──────────────────────────────────────────────────

    private fun setupRecipient() {
        if (!isNurse) {
            binding.tvRecipient.text = getString(R.string.vn_to_your_nurse, patientRoom())
            return
        }
        binding.tvRecipient.text = getString(R.string.vn_loading_rooms)
        binding.fabRecord.isEnabled = false
        lifecycleScope.launch {
            rooms = uploader.listMyPatients()
            if (rooms.isEmpty()) {
                binding.tvRecipient.text = getString(R.string.vn_no_rooms)
                return@launch
            }
            binding.tvRecipient.visibility = View.GONE
            binding.spinnerRoom.visibility = View.VISIBLE
            binding.spinnerRoom.adapter = ArrayAdapter(
                this@SendVoiceNoteActivity,
                android.R.layout.simple_spinner_dropdown_item,
                rooms.map { r -> if (r.patientName.isBlank()) "Room ${r.room}" else "Room ${r.room} · ${r.patientName}" }
            )
            intent.getStringExtra(EXTRA_ROOM)?.let { wanted ->
                val idx = rooms.indexOfFirst { it.room == wanted.trim() }
                if (idx >= 0) binding.spinnerRoom.setSelection(idx)
            }
            binding.fabRecord.isEnabled = true
        }
    }

    private fun setupLanguage() {
        val prefs = getSharedPreferences(ServerUploader.PREFS_NAME, Context.MODE_PRIVATE)
        val checkedId = when (prefs.getString(PREF_LANG, "en")) {
            "hi" -> R.id.btnLangHi
            "de" -> R.id.btnLangDe
            else -> R.id.btnLangEn
        }
        binding.toggleLanguage.check(checkedId)
        binding.toggleLanguage.addOnButtonCheckedListener { _, id, isChecked ->
            if (isChecked) {
                val code = when (id) {
                    R.id.btnLangHi -> "hi"
                    R.id.btnLangDe -> "de"
                    else -> "en"
                }
                prefs.edit().putString(PREF_LANG, code).apply()
            }
        }
    }

    private fun selectedLanguage(): String = when (binding.toggleLanguage.checkedButtonId) {
        R.id.btnLangHi -> "hi"
        R.id.btnLangDe -> "de"
        else -> "en"
    }

    private fun targetRoom(): String? =
        if (isNurse) rooms.getOrNull(binding.spinnerRoom.selectedItemPosition)?.room else patientRoom()

    private fun patientRoom(): String {
        val prefs = getSharedPreferences(ServerUploader.PREFS_NAME, Context.MODE_PRIVATE)
        return UserSession.getRoomNumber(this)
            ?: prefs.getString(ServerUploader.KEY_ROOM_ID, ServerUploader.DEFAULT_ROOM_ID)
            ?: ServerUploader.DEFAULT_ROOM_ID
    }

    // ── Recording ─────────────────────────────────────────────────────────────

    private fun toggleRecording() {
        if (recorder.isRecording()) {
            recorder.stop()
            return
        }
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED) {
            micPermission.launch(Manifest.permission.RECORD_AUDIO)
            return
        }
        startRecording()
    }

    private fun startRecording() {
        stopPreview()
        pendingWav = null
        binding.reviewRow.visibility = View.GONE
        recorder.start(lifecycleScope) {
            recordStartedAt = System.currentTimeMillis()
            binding.fabRecord.setImageResource(R.drawable.ic_vn_stop)
            binding.fabRecord.contentDescription = getString(R.string.vn_stop)
            binding.fabRecord.backgroundTintList =
                ColorStateList.valueOf(ContextCompat.getColor(this, R.color.colorPriorityCritical))
            binding.tvStatus.text = getString(R.string.vn_recording)
            binding.toggleLanguage.isEnabled = false
            binding.spinnerRoom.isEnabled = false
            handler.post(ticker)
        }
    }

    private fun onRecordingFinished(wav: ByteArray, durationMs: Int) {
        handler.removeCallbacks(ticker)
        binding.fabRecord.setImageResource(R.drawable.ic_vn_mic)
        binding.fabRecord.contentDescription = getString(R.string.vn_record)
        binding.fabRecord.backgroundTintList = ColorStateList.valueOf(accentColor())
        binding.toggleLanguage.isEnabled = true
        binding.spinnerRoom.isEnabled = true

        if (wav.size <= 44 || durationMs < 300) {
            binding.tvStatus.text = getString(R.string.vn_no_audio)
            showTimer(0)
            return
        }
        pendingWav = wav
        pendingDurationMs = durationMs
        showTimer(durationMs.toLong())
        binding.tvStatus.text = getString(R.string.vn_recorded)
        binding.reviewRow.visibility = View.VISIBLE
        binding.btnSend.isEnabled = true
    }

    private fun showTimer(elapsedMs: Long) {
        val s = (elapsedMs.coerceIn(0, MAX_MS) / 1000).toInt()
        binding.tvTimer.text = "%d:%02d / 0:30".format(s / 60, s % 60)
    }

    // ── Review ────────────────────────────────────────────────────────────────

    private fun togglePreview() {
        if (player != null) { stopPreview(); return }
        val wav = pendingWav ?: return
        lifecycleScope.launch {
            try {
                val file = withContext(Dispatchers.IO) {
                    File.createTempFile("vn_preview", ".wav", cacheDir).apply { writeBytes(wav) }
                }
                playerFile = file
                player = MediaPlayer().apply {
                    setDataSource(file.absolutePath)
                    setOnCompletionListener { stopPreview() }
                    setOnErrorListener { _, _, _ -> stopPreview(); true }
                    prepare()
                    start()
                }
                binding.btnPreview.text = getString(R.string.vn_stop)
                binding.btnPreview.setIconResource(R.drawable.ic_vn_stop)
            } catch (e: Exception) {
                Toast.makeText(this@SendVoiceNoteActivity, R.string.vn_play_failed, Toast.LENGTH_SHORT).show()
                stopPreview()
            }
        }
    }

    private fun stopPreview() {
        player?.let { runCatching { it.stop() }; it.release() }
        player = null
        playerFile?.delete()
        playerFile = null
        binding.btnPreview.text = getString(R.string.vn_preview)
        binding.btnPreview.setIconResource(R.drawable.ic_vn_play)
    }

    private fun discard() {
        stopPreview()
        pendingWav = null
        binding.reviewRow.visibility = View.GONE
        binding.tvStatus.text = getString(R.string.vn_tap_to_record)
        showTimer(0)
    }

    private fun send() {
        val wav = pendingWav ?: return
        val room = targetRoom() ?: run {
            Toast.makeText(this, R.string.vn_no_rooms, Toast.LENGTH_SHORT).show()
            return
        }
        stopPreview()
        binding.btnSend.isEnabled = false
        binding.fabRecord.isEnabled = false
        binding.tvStatus.text = getString(R.string.vn_sending)
        val alertId = intent.getIntExtra(EXTRA_ALERT_ID, -1).takeIf { it > 0 }
        lifecycleScope.launch {
            val ok = uploader.uploadVoiceNote(
                wavBytes = wav, roomId = room, language = selectedLanguage(),
                durationMs = pendingDurationMs, alertId = alertId,
            )
            if (ok) {
                Toast.makeText(this@SendVoiceNoteActivity, R.string.vn_sent, Toast.LENGTH_SHORT).show()
                finish()
            } else {
                binding.tvStatus.text = getString(R.string.vn_failed)
                binding.btnSend.isEnabled = true
                binding.fabRecord.isEnabled = true
            }
        }
    }
}
