package com.carevoice.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder

/**
 * CallService — minimal foreground service that keeps the microphone and audio
 * stack alive while a WebRTC call is in progress and the app is backgrounded
 * (Android 10+ blocks background mic access without a foreground service typed
 * `microphone`).
 *
 * It does no audio work itself — [WebRtcCallManager] owns the media. This
 * service only holds the foreground notification.
 */
class CallService : Service() {

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForegroundNotification()
        return START_NOT_STICKY
    }

    private fun startForegroundNotification() {
        val channelId = "carevoice_call"
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            if (nm.getNotificationChannel(channelId) == null) {
                nm.createNotificationChannel(
                    NotificationChannel(channelId, "Active Call", NotificationManager.IMPORTANCE_LOW)
                )
            }
        }

        val tapIntent = PendingIntent.getActivity(
            this, 0, Intent(this, CallActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val notification: Notification = Notification.Builder(this, channelId)
            .setContentTitle("CareVoice call in progress")
            .setContentText("Tap to return to the call")
            .setSmallIcon(android.R.drawable.sym_action_call)
            .setContentIntent(tapIntent)
            .setOngoing(true)
            .build()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIF_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        } else {
            startForeground(NOTIF_ID, notification)
        }
    }

    companion object { private const val NOTIF_ID = 4201 }
}
