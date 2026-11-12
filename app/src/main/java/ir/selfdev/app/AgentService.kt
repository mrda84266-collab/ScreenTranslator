package ir.selfdev.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File

/** سرویس پیش‌زمینه: پایتون (عامل + وب‌سرور) را زنده نگه می‌دارد، حتی با صفحه‌ی خاموش. */
class AgentService : Service() {
    companion object {
        @Volatile var started = false
        @Volatile var error: String? = null
    }

    private var wake: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel("agent", "SelfDev", NotificationManager.IMPORTANCE_LOW))
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE
        )
        val n = Notification.Builder(this, "agent")
            .setContentTitle("SelfDev در حال کار است")
            .setContentText("برای باز کردن لمس کن")
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentIntent(open)
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(1, n)
        }
        if (wake == null) {
            wake = getSystemService(PowerManager::class.java)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "selfdev:agent").apply { acquire() }
        }
        if (!started) {
            started = true
            Thread {
                try {
                    for (name in listOf("catalog.json", "models_catalog.json")) {
                        val f = File(filesDir, name)
                        if (!f.exists()) assets.open(name).use { i -> f.outputStream().use { o -> i.copyTo(o) } }
                    }
                    if (!Python.isStarted()) Python.start(AndroidPlatform(this))
                    Python.getInstance().getModule("android_entry")
                        .callAttr("start", filesDir.absolutePath, applicationInfo.nativeLibraryDir)
                } catch (t: Throwable) {
                    error = t.toString()
                    started = false
                }
            }.start()
        }
        return START_STICKY
    }

    override fun onDestroy() {
        wake?.release()
        super.onDestroy()
    }
}
