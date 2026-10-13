package com.carevoice.app

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat

/**
 * Small helper for system notifications. Currently used to alert both nurse and
 * patient when a new voice note arrives for them.
 */
object Notifier {

    private const val CHANNEL_VOICE_NOTES = "voice_notes"
    private const val VN_NOTIFICATION_ID = 4201

    private fun ensureChannel(context: Context) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val mgr = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            if (mgr.getNotificationChannel(CHANNEL_VOICE_NOTES) == null) {
                val ch = NotificationChannel(
                    CHANNEL_VOICE_NOTES,
                    "Voice notes",
                    NotificationManager.IMPORTANCE_HIGH
                ).apply { description = "New voice note received" }
                mgr.createNotificationChannel(ch)
            }
        }
    }

    /**
     * Show a "new voice note" notification. [openActivity] is launched on tap.
     * Silently no-ops if the user has denied the POST_NOTIFICATIONS permission.
     */
    fun voiceNote(context: Context, title: String, body: String, openActivity: Class<*>) {
        ensureChannel(context)
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
}
