package ai.roadpod.app

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.content.ContextCompat

/**
 * Keeps the session, vehicle listeners, parking timer and TTS alive while the navigation app is in front.
 * Found on the Pleos emulator: without it the cached-app freezer froze the process ~3 s after the app
 * went to the background, so the 5-minute parking timer and the WebSocket stopped.
 *
 * Type: mediaPlayback (the radio plays DJ speech) + microphone when RECORD_AUDIO is granted (STT).
 */
class RoadpodService : Service() {

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForegroundCompat()
        val app = application as RoadpodApp
        app.controller.start(app.clientId())
        return START_STICKY
    }

    override fun onDestroy() {
        (application as RoadpodApp).releaseController()   // sends session.ended, closes WS, releases SDKs
        super.onDestroy()
    }

    private fun startForegroundCompat() {
        val nm = getSystemService(NotificationManager::class.java)
        if (nm.getNotificationChannel(CHANNEL) == null) {
            nm.createNotificationChannel(NotificationChannel(CHANNEL, "MY Q 방송", NotificationManager.IMPORTANCE_LOW))
        }
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE,
        )
        val n: Notification = Notification.Builder(this, CHANNEL)
            .setContentTitle("MY Q")
            .setContentText("큐큐 라디오 연결 중")
            .setSmallIcon(android.R.drawable.ic_media_play)
            .setContentIntent(open)
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= 29) {
            var type = ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                type = type or ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
            }
            startForeground(NOTIFICATION_ID, n, type)
        } else {
            startForeground(NOTIFICATION_ID, n)
        }
    }

    companion object {
        private const val CHANNEL = "roadpod_session"
        private const val NOTIFICATION_ID = 1

        /** Call from the foreground activity (Android 12+ forbids starting a foreground service from the background). */
        fun start(context: Context) {
            ContextCompat.startForegroundService(context, Intent(context, RoadpodService::class.java))
        }

        /** Driver ended the broadcast: session ends, notification disappears. */
        fun stop(context: Context) {
            context.stopService(Intent(context, RoadpodService::class.java))
        }
    }
}
