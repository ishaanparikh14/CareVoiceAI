package com.carevoice.app

import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import androidx.core.content.ContextCompat
import androidx.recyclerview.widget.DiffUtil
import androidx.recyclerview.widget.ListAdapter
import androidx.recyclerview.widget.RecyclerView
import com.carevoice.app.databinding.ItemVoiceNoteBinding
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/** Card list for received voice notes: play the original voice, translate, reply. */
class VoiceNoteAdapter(
    private val showRoom: Boolean,
    private val onPlay: (VoiceNote) -> Unit,
    private val onTranslate: (VoiceNote) -> Unit,
    private val onReply: (VoiceNote) -> Unit,
) : ListAdapter<VoiceNote, VoiceNoteAdapter.ViewHolder>(DIFF) {

    /** Note id currently playing (button shows Stop). */
    var playingId: Int? = null
    /** Finished translations, keyed by note id. */
    val translations = mutableMapOf<Int, String>()
    /** Notes with a translation in progress. */
    val translating = mutableSetOf<Int>()

    fun refresh(noteId: Int) {
        val pos = currentList.indexOfFirst { it.id == noteId }
        if (pos >= 0) notifyItemChanged(pos)
    }

    inner class ViewHolder(private val b: ItemVoiceNoteBinding) : RecyclerView.ViewHolder(b.root) {

        fun bind(note: VoiceNote) {
            val ctx = b.root.context
            val sender = note.senderName.ifBlank { note.senderRole }
            b.tvHeader.text = if (showRoom) ctx.getString(R.string.vn_header_room, note.roomId, sender) else sender

            val role = ctx.getString(if (note.senderRole == "nurse") R.string.vn_from_nurse else R.string.vn_from_patient)
            val lang = ctx.getString(if (note.language == "hi") R.string.vn_lang_hindi else R.string.vn_lang_english)
            b.tvMeta.text = listOf(role, lang, formatDuration(note.durationMs), formatTime(note.createdAt))
                .filter { it.isNotBlank() }
                .joinToString("  ·  ")

            val transcript = note.originalText?.trim().orEmpty()
            b.tvTranscript.text = if (transcript.isNotEmpty()) "“$transcript”" else ctx.getString(R.string.vn_no_transcript)

            // Original voice
            val playing = playingId == note.id
            // Nurse view (showRoom) uses teal; patient view keeps indigo.
            val accent = ContextCompat.getColor(
                ctx, if (showRoom) R.color.colorLoginNurseAccent else R.color.colorLoginPatientAccent
            )
            b.btnPlay.backgroundTintList = android.content.res.ColorStateList.valueOf(accent)
            b.btnPlay.text = ctx.getString(if (playing) R.string.vn_stop else R.string.vn_play_voice)
            b.btnPlay.icon = ContextCompat.getDrawable(ctx, if (playing) R.drawable.ic_vn_stop else R.drawable.ic_vn_play)
            b.btnPlay.setOnClickListener { onPlay(note) }

            // Translation
            val translated = translations[note.id]
            val target = NoteTranslator.targetFor(note.language)
            b.tvTranslation.visibility = if (translated != null) View.VISIBLE else View.GONE
            b.tvTranslation.text = translated.orEmpty()
            b.btnTranslate.isEnabled = transcript.isNotEmpty() && note.id !in translating
            b.btnTranslate.text = ctx.getString(
                when {
                    note.id in translating -> R.string.vn_translating
                    translated != null     -> R.string.vn_listen_translation
                    target == "hi"         -> R.string.vn_translate_to_hi
                    else                   -> R.string.vn_translate_to_en
                }
            )
            b.btnTranslate.setOnClickListener { onTranslate(note) }

            b.btnReply.setOnClickListener { onReply(note) }
        }

        private fun formatDuration(ms: Int): String {
            if (ms <= 0) return ""
            val s = (ms + 500) / 1000
            return "%d:%02d".format(s / 60, s % 60)
        }

        private fun formatTime(iso: String): String = try {
            DateTimeFormatter.ofPattern("d MMM, HH:mm")
                .withZone(ZoneId.systemDefault())
                .format(OffsetDateTime.parse(iso).toInstant())
        } catch (_: Exception) {
            ""
        }
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): ViewHolder =
        ViewHolder(ItemVoiceNoteBinding.inflate(LayoutInflater.from(parent.context), parent, false))

    override fun onBindViewHolder(holder: ViewHolder, position: Int) = holder.bind(getItem(position))

    companion object {
        private val DIFF = object : DiffUtil.ItemCallback<VoiceNote>() {
            override fun areItemsTheSame(old: VoiceNote, new: VoiceNote) = old.id == new.id
            override fun areContentsTheSame(old: VoiceNote, new: VoiceNote) = old == new
        }
    }
}
