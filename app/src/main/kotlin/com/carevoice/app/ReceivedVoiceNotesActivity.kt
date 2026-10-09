package com.carevoice.app

import android.content.Context
import android.content.Intent
import android.media.MediaPlayer
import android.os.Bundle
import android.view.Menu
import android.view.MenuItem
import android.view.View
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import androidx.recyclerview.widget.LinearLayoutManager
import com.carevoice.app.databinding.ActivityVoiceNotesReceivedBinding
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

/**
 * Received voice notes (nurse and patient).
 *
 * Nurse: notes from every assigned room (or one room when opened from an alert).
 * Patient: notes the nurse sent to the patient's room.
 * Each card plays the sender's original voice, translates the transcript
 * English ↔ Hindi on-device, and offers a reply.
 */
class ReceivedVoiceNotesActivity : AppCompatActivity() {

    companion object {
        private const val EXTRA_ROOM = "room"
        private const val MENU_REFRESH = 1

        fun open(context: Context, room: String? = null) {
            context.startActivity(
                Intent(context, ReceivedVoiceNotesActivity::class.java)
                    .apply { room?.let { putExtra(EXTRA_ROOM, it) } }
            )
        }
    }

    private lateinit var binding: ActivityVoiceNotesReceivedBinding
    private lateinit var adapter: VoiceNoteAdapter
    private lateinit var uploader: ServerUploader
    private lateinit var tts: TtsManager
    private val translator = NoteTranslator()
    private var player: MediaPlayer? = null
    private var playerFile: File? = null

    private val role by lazy { UserSession.getRole(this) ?: "patient" }
    private val isNurse get() = role == "nurse"
    private val roomFilter by lazy { intent.getStringExtra(EXTRA_ROOM) }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityVoiceNotesReceivedBinding.inflate(layoutInflater)
        setContentView(binding.root)
        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        title = roomFilter?.let { getString(R.string.vn_received_room, it) } ?: getString(R.string.vn_received)
        if (isNurse) applyNurseAccent()

        uploader = ServerUploader(this)
        tts = TtsManager(this)
        adapter = VoiceNoteAdapter(
            showRoom = isNurse,
            onPlay = ::togglePlay,
            onTranslate = ::translate,
            onReply = { note -> SendVoiceNoteActivity.open(this, note.roomId, note.alertId) },
        )
        binding.rvNotes.layoutManager = LinearLayoutManager(this)
        binding.rvNotes.adapter = adapter
    }

    override fun onResume() {
        super.onResume()
        load()
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menu.add(Menu.NONE, MENU_REFRESH, Menu.NONE, R.string.nurse_refresh)
            .setShowAsAction(MenuItem.SHOW_AS_ACTION_ALWAYS)
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean = when (item.itemId) {
        android.R.id.home -> { finish(); true }
        MENU_REFRESH      -> { load(); true }
        else              -> super.onOptionsItemSelected(item)
    }

    override fun onStop() {
        super.onStop()
        stopPlayback()
    }

    override fun onDestroy() {
        super.onDestroy()
        translator.close()
        tts.shutdown()
    }

    /** Nurse screens use the teal brand colour instead of the patient indigo. */
    private fun applyNurseAccent() {
        supportActionBar?.setBackgroundDrawable(
            android.graphics.drawable.ColorDrawable(getColor(R.color.colorLoginNurseAccent))
        )
        window.statusBarColor = getColor(R.color.cv_nurse_dark)
        binding.progress.indeterminateTintList =
            android.content.res.ColorStateList.valueOf(getColor(R.color.colorLoginNurseAccent))
    }

    // ── Loading ───────────────────────────────────────────────────────────────

    private fun load() {
        binding.progress.visibility = View.VISIBLE
        binding.tvEmpty.visibility = View.GONE
        lifecycleScope.launch {
            val notes = when {
                roomFilter != null -> uploader.listVoiceNotes(roomFilter!!)
                isNurse            -> uploader.inboxVoiceNotes()
                else               -> uploader.listVoiceNotes(ownRoom())
            }
            // "Received" = sent by the other side.
            val received = notes.filter { it.senderRole != role }
            binding.progress.visibility = View.GONE
            binding.tvEmpty.visibility = if (received.isEmpty()) View.VISIBLE else View.GONE
            adapter.submitList(received)
        }
    }

    private fun ownRoom(): String {
        val prefs = getSharedPreferences(ServerUploader.PREFS_NAME, Context.MODE_PRIVATE)
        return UserSession.getRoomNumber(this)
            ?: prefs.getString(ServerUploader.KEY_ROOM_ID, ServerUploader.DEFAULT_ROOM_ID)
            ?: ServerUploader.DEFAULT_ROOM_ID
    }

    // ── Original voice playback ───────────────────────────────────────────────

    private fun togglePlay(note: VoiceNote) {
        val wasPlaying = adapter.playingId == note.id
        stopPlayback()
        if (wasPlaying) return

        adapter.playingId = note.id
        adapter.refresh(note.id)
        lifecycleScope.launch {
            val bytes = uploader.fetchVoiceNoteAudio(note.id)
            if (adapter.playingId != note.id) return@launch   // user moved on
            if (bytes == null) {
                Toast.makeText(this@ReceivedVoiceNotesActivity, R.string.vn_play_failed, Toast.LENGTH_SHORT).show()
                stopPlayback()
                return@launch
            }
            try {
                val file = withContext(Dispatchers.IO) {
                    File.createTempFile("vn_${note.id}", ".wav", cacheDir).apply { writeBytes(bytes) }
                }
                playerFile = file
                player = MediaPlayer().apply {
                    setDataSource(file.absolutePath)
                    setOnCompletionListener { stopPlayback() }
                    setOnErrorListener { _, _, _ -> stopPlayback(); true }
                    prepare()
                    start()
                }
            } catch (e: Exception) {
                Toast.makeText(this@ReceivedVoiceNotesActivity, R.string.vn_play_failed, Toast.LENGTH_SHORT).show()
                stopPlayback()
            }
        }
    }

    private fun stopPlayback() {
        player?.let { runCatching { it.stop() }; it.release() }
        player = null
        playerFile?.delete()
        playerFile = null
        val id = adapter.playingId
        adapter.playingId = null
        if (id != null) adapter.refresh(id)
    }

    // ── Translation ───────────────────────────────────────────────────────────

    private fun translate(note: VoiceNote) {
        val target = NoteTranslator.targetFor(note.language)
        adapter.translations[note.id]?.let { done ->
            tts.speakVoiceNote(done, target, note.id)   // "Listen to translation"
            return
        }
        val text = note.originalText?.trim().orEmpty()
        if (text.isEmpty()) return

        adapter.translating.add(note.id)
        adapter.refresh(note.id)
        Toast.makeText(this, R.string.vn_translate_first_use, Toast.LENGTH_SHORT).show()
        translator.translate(text, note.language, target) { result ->
            adapter.translating.remove(note.id)
            if (result == null) {
                Toast.makeText(this, R.string.vn_translate_failed, Toast.LENGTH_LONG).show()
            } else {
                adapter.translations[note.id] = result
                stopPlayback()
                tts.speakVoiceNote(result, target, note.id)
            }
            adapter.refresh(note.id)
        }
    }
}
