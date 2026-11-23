package com.carevoice.app

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.RingtoneManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat

/**
 * System notifications:
 *  - voice notes (nurse + patient)
 *  - nurse request alerts (high importance; Critical uses a separate alarm-style
 *    channel) — shown when the nurse app is not in front, so requests are not
 *    missed while the phone is in a pocket. Delivered over the app's live socket
 *    / poll kept alive by [NurseAlertService] (no Firebase push yet).
 */
object Notifier {

    private const val CHANNEL_VOICE_NOTES = "voice_notes"
    const val CHANNEL_ALERTS = "nurse_alerts_v1"
    const val CHANNEL_CRITICAL = "nurse_critical_v1"
    const val CHANNEL_ON_DUTY = "nurse_on_duty_v1"
    // 4201 is CallService's foreground notification — keep voice notes separate.
    private const val VN_NOTIFICATION_ID = 4202
    private const val ALERT_ID_BASE = 50_000

    private fun ensureChannels(context: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val mgr = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (mgr.getNotificationChannel(CHANNEL_VOICE_NOTES) == null) {
            mgr.createNotificationChannel(NotificationChannel(
                CHANNEL_VOICE_NOTES, "Voice notes", NotificationManager.IMPORTANCE_HIGH
            ).apply { description = "New voice note received" })
        }
        if (mgr.getNotificationChannel(CHANNEL_ALERTS) == null) {
            mgr.createNotificationChannel(NotificationChannel(
                CHANNEL_ALERTS, "Patient requests", NotificationManager.IMPORTANCE_HIGH
            ).apply {
                description = "New requests from your patients"
                enableVibration(true)
                vibrationPattern = longArrayOf(0, 250, 150, 250)
            })
        }
        if (mgr.getNotificationChannel(CHANNEL_CRITICAL) == null) {
            mgr.createNotificationChannel(NotificationChannel(
                CHANNEL_CRITICAL, "Critical requests", NotificationManager.IMPORTANCE_HIGH
            ).apply {
                description = "Emergencies — loud and insistent"
                enableVibration(true)
                vibrationPattern = longArrayOf(0, 600, 200, 600, 200, 600)
                setSound(
                    RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM),
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ALARM)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                        .build()
                )
            })
        }
        if (mgr.getNotificationChannel(CHANNEL_ON_DUTY) == null) {
            mgr.createNotificationChannel(NotificationChannel(
                CHANNEL_ON_DUTY, "On duty (background)", NotificationManager.IMPORTANCE_MIN
            ).apply { description = "Keeps request alerts working while the app is in the background" })
        }
    }

    fun ensure(context: Context) = ensureChannels(context)

    /**
     * Show a "new voice note" notification. [openActivity] is launched on tap.
     * Silently no-ops if the user has denied the POST_NOTIFICATIONS permission.
     */
    fun voiceNote(context: Context, title: String, body: String, openActivity: Class<*>) {
        ensureChannels(context)
        val tapIntent = PendingIntent.getActivity(
            context, 0,
            Intent(context, openActivity).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
            },
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val notif = NotificationCompat.Builder(context, CHANNEL_VOICE_NOTES)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentTitle(title)
            .setContentText(body)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .setContentIntent(tapIntent)
            .build()
        try {
            NotificationManagerCompat.from(context).notify(VN_NOTIFICATION_ID, notif)
        } catch (_: SecurityException) {
            // POST_NOTIFICATIONS denied — ignore.
        }
    }

    /** One notification per request, replaced on update, cancelled on ACK. */
    fun alert(
        context: Context, alertId: Int, priority: String, roomId: String,
        patientName: String?, summary: String?, escalated: Boolean = false,
    ) {
        ensureChannels(context)
        val critical = priority == "Critical"
        val tapIntent = PendingIntent.getActivity(
            context, alertId,
            Intent(context, NurseActivity::class.java).apply {
                // Reuse the running dashboard (a second instance would open a
                // second alert socket for the same nurse).
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP or
                    Intent.FLAG_ACTIVITY_SINGLE_TOP
            },
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val who = patientName ?: "Patient"
        val title = buildString {
            if (escalated) append("⬆ ")
            append(if (critical) "EMERGENCY" else priority)
            append(" · Room ").append(roomId)
        }
        val body = if (!summary.isNullOrBlank()) "$who — $summary" else "$who is calling"
        val notif = NotificationCompat.Builder(context, if (critical) CHANNEL_CRITICAL else CHANNEL_ALERTS)
            .setSmallIcon(android.R.drawable.ic_dialog_alert)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setPriority(if (critical) NotificationCompat.PRIORITY_MAX else NotificationCompat.PRIORITY_HIGH)
            .setCategory(if (critical) NotificationCompat.CATEGORY_ALARM else NotificationCompat.CATEGORY_MESSAGE)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setAutoCancel(true)
            .setOnlyAlertOnce(!escalated)
            .setContentIntent(tapIntent)
            .build()
        try {
            NotificationManagerCompat.from(context).notify(ALERT_ID_BASE + alertId, notif)
        } catch (_: SecurityException) { }
    }

    fun cancelAlert(context: Context, alertId: Int) {
        try { NotificationManagerCompat.from(context).cancel(ALERT_ID_BASE + alertId) } catch (_: Exception) {}
    }
}
