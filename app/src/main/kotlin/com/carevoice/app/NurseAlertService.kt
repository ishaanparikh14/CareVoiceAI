package com.carevoice.app

import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat

/**
 * NurseAlertService — foreground service that keeps the nurse app's process
 * (its live alert socket and 15 s poll) running while the app is in the
 * background, so new requests still raise a notification.
 *
 * This is the no-Firebase option: it works while the phone has network and
 * the service is running. Real push (FCM) additionally survives the app being
 * force-stopped and deep Doze; that needs the hospital's Firebase project.
 */
class NurseAlertService : Service() {

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        Notifier.ensure(this)
        val tap = PendingIntent.getActivity(
            this, 0,
            Intent(this, NurseActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val n = NotificationCompat.Builder(this, Notifier.CHANNEL_ON_DUTY)
            .setSmallIcon(android.R.drawable.ic_popup_reminder)
            .setContentTitle("CareVoice — on duty")
            .setContentText("You'll be notified of new patient requests")
            .setOngoing(true)
            .setContentIntent(tap)
            .setPriority(NotificationCompat.PRIORITY_MIN)
            .build()
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
            } else {
                startForeground(NOTIF_ID, n)
            }
        } catch (e: Exception) {
            Log.w("NurseAlertService", "Could not start foreground: ${e.message}")
            stopSelf()
        }
        // Not sticky: the socket lives in NurseActivity, so a service restarted
        // alone after process death would show "on duty" without listening.
        return START_NOT_STICKY
    }

    companion object {
        private const val NOTIF_ID = 4300

        fun start(ctx: Context) {
            try {
                ContextCompat.startForegroundService(ctx, Intent(ctx, NurseAlertService::class.java))
            } catch (e: Exception) {
                Log.w("NurseAlertService", "start failed: ${e.message}")
            }
        }

        fun stop(ctx: Context) {
            try { ctx.stopService(Intent(ctx, NurseAlertService::class.java)) } catch (_: Exception) {}
        }
    }
}
