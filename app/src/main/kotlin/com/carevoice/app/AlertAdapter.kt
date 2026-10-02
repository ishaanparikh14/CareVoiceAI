package com.carevoice.app

import android.content.res.ColorStateList
import android.os.Handler
import android.os.Looper
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
 * ACK vs ATTEND are separate, independent actions (see database.py /
 * escalation_manager.py): ACK only means a nurse has seen the alert; only
 * ATTEND stops the Urgent→Critical auto-escalation timer.
 *
 * @param onAck    Called with the alert ID when the nurse taps ACK.
 * @param onAttend Called with the alert ID when the nurse taps MARK ATTENDED.
 */
class AlertAdapter(
    private val onAck: (alertId: Int, nurseName: String) -> Unit,
    private val onAttend: (alertId: Int, nurseName: String) -> Unit,
    private val onHearSummary: (alert: AlertModel) -> Unit,
    private val onVoiceReply: (alert: AlertModel) -> Unit,
    private val onPlayPatientVoice: (alert: AlertModel) -> Unit
) : ListAdapter<AlertModel, AlertAdapter.ViewHolder>(DIFF) {

    // Nurse name injected by NurseActivity after login
    var nurseName: String = "Nurse"

    // ── ViewHolder ────────────────────────────────────────────────────────────

    inner class ViewHolder(private val b: ItemAlertBinding) :
        RecyclerView.ViewHolder(b.root) {

        private val countdownHandler = Handler(Looper.getMainLooper())
        private var countdownRunnable: Runnable? = null

        fun bind(alert: AlertModel) {
            stopCountdown()

            val isEscalated = alert.escalated && !alert.attended

            // ── Priority badge ────────────────────────────────────────────────
            val (labelColor, bgColor) = priorityColors(alert, isEscalated)
            b.tvPriority.text = if (isEscalated)
                b.root.context.getString(R.string.nurse_priority_escalated)
            else
                alert.priority.uppercase()
            b.tvPriority.setTextColor(ContextCompat.getColor(b.root.context, labelColor))
            b.tvPriority.backgroundTintList = ColorStateList.valueOf(
                ContextCompat.getColor(b.root.context, bgColor)
            )

            // ── Room + time ───────────────────────────────────────────────────
            b.tvRoom.text = "${alert.patientName} • Room ${alert.roomId}"
            b.tvTime.text = formatTime(alert.createdAt)

            // ── Intent (or escalation subtitle) ─────────────────────────────────
            b.tvIntent.text = if (isEscalated)
                b.root.context.getString(R.string.nurse_escalation_subtitle)
            else
                alert.intent

           // ── Response timer ────────────────────────────────────────────────────────
           // The timer starts at alert creation and continues until ATTEND.
           // ACK does NOT stop, reset, or pause the timer.
           //
           // For an unattended alert:
           //   "Unattended: 02:34"
           //
           // For an attended alert:
           //   "Response time: 03:12"
           //
           // The server remains authoritative for escalation. This timer is only
           // a display of how long the patient has been waiting.
            if (!alert.attended) {
                b.tvDistress.visibility = View.VISIBLE
                startResponseTimer(alert.createdAt)
            } else {
                b.tvDistress.visibility = View.VISIBLE
                b.tvDistress.text = "Response time: ${calculateResponseTime(alert.createdAt, alert.attendedAt)}"
            }

            // ── Transcript ────────────────────────────────────────────────────
            b.tvTranscript.text = "\"${alert.transcript}\""

            // ── ACK — Urgent only, and only before it's been attended.
            //    Critical (AI-generated or escalated) skips straight to
            //    MARK ATTENDED per spec: no ACK button shown. ────────────────
            val showAck = !alert.acknowledged && !alert.attended
            b.btnAck.visibility = if (showAck) View.VISIBLE else View.GONE
            if (showAck) {
                b.btnAck.isEnabled = true
                b.btnAck.setOnClickListener {
                    b.btnAck.isEnabled = false // prevent double-tap
                    onAck(alert.id, nurseName)
                }
            }
            b.tvAckedBy.visibility = if (alert.acknowledged) View.VISIBLE else View.GONE
            b.tvAckedBy.text = "✓ ACK: ${alert.ackedBy ?: "acknowledged"}"

            // ── MARK ATTENDED — the only action that stops escalation.
            //    Shown for any unattended Urgent, Critical, or Escalated alert.
            //    Never auto-triggered by ACK. ──────────────────────────────────
            val showAttend = !alert.attended
            b.btnAttend.visibility = if (showAttend) View.VISIBLE else View.GONE
            if (showAttend) {
                b.btnAttend.isEnabled = true
                b.btnAttend.setOnClickListener {
                    b.btnAttend.isEnabled = false // prevent double-tap
                    onAttend(alert.id, nurseName)
                }
            }
            b.tvAttendedBy.visibility = if (alert.attended) View.VISIBLE else View.GONE
            b.tvAttendedBy.text = "✓ Attended: ${alert.attendedBy ?: "attended"}"

            // Detailed NLP-style summary is on demand for Routine/Urgent.
            // Critical already includes its summary in the automatic TTS.
            b.btnHearSummary.visibility = if (alert.priority == "Critical" || alert.escalated) View.GONE else View.VISIBLE
            b.btnHearSummary.setOnClickListener { onHearSummary(alert) }
            b.root.setOnClickListener {
                if (!alert.attended && alert.priority != "Critical" && !alert.escalated) {
                    onHearSummary(alert)
                }
            }

            // Nurse can send a recorded voice reply back to the patient's room.
            b.btnVoiceReply.visibility = if (alert.attended) View.GONE else View.VISIBLE
            b.btnVoiceReply.setOnClickListener { onVoiceReply(alert) }
            b.btnPlayPatientVoice.visibility = View.VISIBLE
            b.btnPlayPatientVoice.setOnClickListener { onPlayPatientVoice(alert) }

            // Dim fully-resolved (attended) cards to de-emphasise them.
            b.root.alpha = if (alert.attended) 0.6f else 1f
        }

        /** Cancel any pending countdown tick — call when the view is rebound or recycled. */
        fun stopCountdown() {
            countdownRunnable?.let { countdownHandler.removeCallbacks(it) }
            countdownRunnable = null
        }

        /**
        * Displays the total time the patient has been waiting for attendance.
        *
        * IMPORTANT:
        * - Starts from the original alert creation time.
        * - ACK does not affect this timer.
        * - Urgent → Critical does not reset this timer.
        * - Timer stops only when ATTEND is recorded.
        *
        * This is display-only. The backend remains authoritative for escalation.
        */
        private fun startResponseTimer(createdAtIso: String) {
            val tv = b.tvDistress

            val createdMs = try {
                Instant.parse(createdAtIso).toEpochMilli()
            } catch (_: Exception) {
                tv.text = "Response time: --:--"
                return
            }

            val tick = object : Runnable {
                override fun run() {
                    val elapsedMs = System.currentTimeMillis() - createdMs

                    val totalSec = maxOf(0L, elapsedMs / 1000L)
                    val minutes = totalSec / 60
                    val seconds = totalSec % 60

                    tv.text = String.format(
                        "Unattended: %02d:%02d",
                        minutes,
                        seconds
                    )

                    countdownRunnable = this
                    countdownHandler.postDelayed(this, 1000)
                }
            }

            countdownRunnable = tick
            countdownHandler.post(tick)
        }

        /**
        * Calculates the final patient response time from alert creation until
        * the nurse actually attended the patient.
        */
        private fun calculateResponseTime(
            createdAtIso: String,
            attendedAtIso: String?
        ): String {
            if (attendedAtIso == null) {
                return "--:--"
            }

            return try {
                val createdMs = Instant.parse(createdAtIso).toEpochMilli()
                val attendedMs = Instant.parse(attendedAtIso).toEpochMilli()

                val elapsedMs = maxOf(0L, attendedMs - createdMs)
                val totalSec = elapsedMs / 1000L

                val minutes = totalSec / 60
                val seconds = totalSec % 60

                String.format("%02d:%02d", minutes, seconds)
            } catch (_: Exception) {
                "--:--"
            }
        }

        /** Maps the alert's rendered state to (text color res, background color res). */
        private fun priorityColors(alert: AlertModel, isEscalated: Boolean): Pair<Int, Int> = when {
            isEscalated -> Pair(R.color.colorPriorityCriticalEscalation, R.color.colorPriorityCriticalBg)
            alert.priority == "Critical" -> Pair(R.color.colorPriorityCritical, R.color.colorPriorityCriticalBg)
            alert.priority == "Urgent"   -> Pair(R.color.colorPriorityUrgent,   R.color.colorPriorityUrgentBg)
            else                          -> Pair(R.color.colorPriorityRoutine,  R.color.colorPriorityRoutineBg)
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

    override fun onViewRecycled(holder: ViewHolder) {
        super.onViewRecycled(holder)
        holder.stopCountdown() // avoid ticking a countdown against a recycled/reused view
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
