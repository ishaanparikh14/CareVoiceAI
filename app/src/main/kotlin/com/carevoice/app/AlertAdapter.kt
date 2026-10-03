package com.carevoice.app

import android.content.res.ColorStateList
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import androidx.core.content.ContextCompat
import androidx.recyclerview.widget.DiffUtil
import androidx.recyclerview.widget.ListAdapter
import androidx.recyclerview.widget.RecyclerView
import com.carevoice.app.databinding.ItemAlertBinding
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/**
 * AlertAdapter — RecyclerView adapter for nurse dashboard alert cards.
 *
 * Uses [ListAdapter] + [DiffUtil] so only changed cards re-render when the
 * list updates (either from a WebSocket push or a manual refresh).
 *
 * @param onAck  Called with the alert ID when the nurse taps ACK.
 */
class AlertAdapter(
    private val onAck: (alertId: Int, nurseName: String) -> Unit,
    private val onCall: (roomId: String) -> Unit = {},
    private val onVoiceNotes: (roomId: String, alertId: Int) -> Unit = { _, _ -> }
) : ListAdapter<AlertModel, AlertAdapter.ViewHolder>(DIFF) {

    // Nurse name injected by NurseActivity after login
    var nurseName: String = "Nurse"

    // ── ViewHolder ────────────────────────────────────────────────────────────

    inner class ViewHolder(private val b: ItemAlertBinding) :
        RecyclerView.ViewHolder(b.root) {

        fun bind(alert: AlertModel) {
            // ── Priority badge ────────────────────────────────────────────────
            val (labelColor, bgColor) = priorityColors(alert.priority)
            b.tvPriority.text = alert.priority.uppercase()
            b.tvPriority.setTextColor(ContextCompat.getColor(b.root.context, labelColor))
            b.tvPriority.backgroundTintList = ColorStateList.valueOf(
                ContextCompat.getColor(b.root.context, bgColor)
            )
            b.viewPriorityStrip.backgroundTintList = ColorStateList.valueOf(
                ContextCompat.getColor(b.root.context, labelColor)
            )

            // ── Room + time ───────────────────────────────────────────────────
            b.tvRoom.text = "Room ${alert.roomId}"
            b.tvTime.text = formatTime(alert.createdAt)

            // ── Intent ────────────────────────────────────────────────────────
            // Mark auto-escalated alerts (Urgent → Critical after timeout).
            b.tvIntent.text = if (alert.escalated) "⬆ ${alert.intent} (escalated)" else alert.intent

            // ── Emotion chip (emotional intelligence) ─────────────────────────
            if (!alert.emotion.isNullOrEmpty()) {
                b.tvDistress.visibility = View.VISIBLE
                b.tvDistress.text = alert.emotion.uppercase()
            } else {
                b.tvDistress.visibility = View.GONE
            }

            // ── NLP summary line (patient + what they need) ──────────────────
            val who = alert.patientName ?: "Patient"
            val summaryLine = when {
                !alert.summary.isNullOrEmpty()  -> "$who — ${alert.summary}"
                else                            -> "$who in Room ${alert.roomId} is calling"
            }
            b.tvSummary.text = summaryLine
            b.tvSummary.visibility = View.VISIBLE

            // ── Transcript ────────────────────────────────────────────────────
            b.tvTranscript.text = "\"${alert.transcript}\""

            // ── Call patient ──────────────────────────────────────────────────
            b.btnCall.setOnClickListener { onCall(alert.roomId) }

            // ── Voice notes (long-press the card) ─────────────────────────────
            b.root.setOnLongClickListener { onVoiceNotes(alert.roomId, alert.id); true }

            // ── ACK state ─────────────────────────────────────────────────────
            if (alert.acknowledged) {
                b.btnAck.visibility  = View.GONE
                b.tvAckedBy.visibility = View.VISIBLE
                b.tvAckedBy.text = "✓ ${alert.ackedBy ?: "acknowledged"}"
                // Dim the card background to de-emphasise resolved alerts
                b.root.alpha = 0.6f
            } else {
                b.btnAck.visibility  = View.VISIBLE
                b.tvAckedBy.visibility = View.GONE
                b.root.alpha = 1f

                b.btnAck.setOnClickListener {
                    // Optimistically disable the button to prevent double-tap
                    b.btnAck.isEnabled = false
                    onAck(alert.id, nurseName)
                }
            }
        }

        /** Maps priority string to (text color res, background color res) pair. */
        private fun priorityColors(priority: String): Pair<Int, Int> = when (priority) {
            "Critical" -> Pair(R.color.colorPriorityCritical, R.color.colorPriorityCriticalBg)
            "Urgent"   -> Pair(R.color.colorPriorityUrgent,   R.color.colorPriorityUrgentBg)
            else       -> Pair(R.color.colorPriorityRoutine,  R.color.colorPriorityRoutineBg)
        }

        /**
         * Parses ISO-8601 UTC timestamp and formats it as "HH:mm" in the
         * device's local timezone so times are immediately meaningful to nurses.
         */
        private fun formatTime(iso: String): String = try {
            val instant = Instant.parse(iso)
            DateTimeFormatter.ofPattern("HH:mm")
                .withZone(ZoneId.systemDefault())
                .format(instant)
        } catch (_: Exception) {
            iso.take(5) // fallback: first 5 chars of whatever string we got
        }
    }

    // ── Adapter overrides ─────────────────────────────────────────────────────

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): ViewHolder {
        val binding = ItemAlertBinding.inflate(
            LayoutInflater.from(parent.context), parent, false
        )
        return ViewHolder(binding)
    }

    override fun onBindViewHolder(holder: ViewHolder, position: Int) {
        holder.bind(getItem(position))
    }

    // ── DiffUtil ──────────────────────────────────────────────────────────────

    companion object {
        private val DIFF = object : DiffUtil.ItemCallback<AlertModel>() {
            override fun areItemsTheSame(old: AlertModel, new: AlertModel) =
                old.id == new.id

            override fun areContentsTheSame(old: AlertModel, new: AlertModel) =
                old == new
        }
    }
}
