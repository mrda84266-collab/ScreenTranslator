package ir.selfdev.app

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.OpenableColumns
import android.provider.Settings
import android.webkit.JavascriptInterface
import android.webkit.JsResult
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import java.io.File
import java.net.HttpURLConnection
import java.net.URL

class MainActivity : Activity() {
    private lateinit var web: WebView
    private val pickCode = 42

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        web = WebView(this)
        setContentView(web)
        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true
        web.webViewClient = WebViewClient()
        web.webChromeClient = object : WebChromeClient() {
            override fun onJsAlert(view: WebView, url: String, message: String, result: JsResult): Boolean {
                AlertDialog.Builder(this@MainActivity).setMessage(message)
                    .setPositiveButton("باشه") { _, _ -> result.confirm() }
                    .setOnCancelListener { result.cancel() }.show()
                return true
            }

            override fun onJsConfirm(view: WebView, url: String, message: String, result: JsResult): Boolean {
                AlertDialog.Builder(this@MainActivity).setMessage(message)
                    .setPositiveButton("بله") { _, _ -> result.confirm() }
                    .setNegativeButton("خیر") { _, _ -> result.cancel() }
                    .setOnCancelListener { result.cancel() }.show()
                return true
            }
        }
        web.addJavascriptInterface(Bridge(), "Android")
        showHtml("<h3 style='font-family:sans-serif;text-align:center;margin-top:30vh'>در حال راه‌اندازی…</h3>")

        if (Build.VERSION.SDK_INT >= 33) requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        askBatteryOnce()
        startForegroundService(Intent(this, AgentService::class.java))
        waitForServer()
    }

    private fun showHtml(body: String) {
        web.loadDataWithBaseURL(null, "<html dir='rtl'><meta name='viewport' content='width=device-width'>$body</html>", "text/html", "utf-8", null)
    }

    private fun askBatteryOnce() {
        val sp = getSharedPreferences("s", MODE_PRIVATE)
        if (sp.getBoolean("asked", false)) return
        sp.edit().putBoolean("asked", true).apply()
        if (!getSystemService(PowerManager::class.java).isIgnoringBatteryOptimizations(packageName)) {
            try {
                startActivity(
                    Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName"))
                )
            } catch (e: Exception) {
            }
        }
    }

    private fun waitForServer() {
        Thread {
            var tries = 0
            while (true) {
                try {
                    val c = URL("http://127.0.0.1:8000/api/state").openConnection() as HttpURLConnection
                    c.connectTimeout = 1000
                    c.readTimeout = 2000
                    if (c.responseCode == 200) {
                        runOnUiThread { web.loadUrl("http://127.0.0.1:8000/") }
                        return@Thread
                    }
                } catch (e: Exception) {
                }
                tries++
                val err = AgentService.error
                if (err != null) {
                    runOnUiThread { showHtml("<pre dir='ltr' style='white-space:pre-wrap;font-size:12px'>خطای راه‌اندازی:\n${err.take(1500)}</pre>") }
                    return@Thread
                }
                if (tries == 90) runOnUiThread { showHtml("<h3 style='text-align:center;margin-top:30vh'>سرور بالا نیامد. اپ را ببند و دوباره باز کن.</h3>") }
                Thread.sleep(1000)
            }
        }.start()
    }

    /** وارد کردن فایل .gguf از حافظه‌ی گوشی (برای وقتی دانلود مستقیم ممکن نیست) */
    inner class Bridge {
        @JavascriptInterface
        fun pickModel() {
            runOnUiThread {
                val i = Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
                    addCategory(Intent.CATEGORY_OPENABLE)
                    type = "*/*"
                }
                startActivityForResult(i, pickCode)
            }
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        val uri = data?.data ?: return
        if (requestCode != pickCode || resultCode != RESULT_OK) return
        var name = "imported.gguf"
        contentResolver.query(uri, null, null, null, null)?.use {
            if (it.moveToFirst()) {
                val idx = it.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                if (idx >= 0) name = it.getString(idx)
            }
        }
        if (!name.endsWith(".gguf")) {
            Toast.makeText(this, "فقط فایل .gguf", Toast.LENGTH_LONG).show()
            return
        }
        val safe = File(name).name
        Toast.makeText(this, "در حال کپی $safe … (چند دقیقه طول می‌کشد)", Toast.LENGTH_LONG).show()
        Thread {
            try {
                val dir = File(filesDir, "models").apply { mkdirs() }
                val part = File(dir, "$safe.part")
                contentResolver.openInputStream(uri)!!.use { i -> part.outputStream().use { o -> i.copyTo(o, 1 shl 20) } }
                part.renameTo(File(dir, safe))
                runOnUiThread { Toast.makeText(this, "$safe اضافه شد", Toast.LENGTH_LONG).show() }
            } catch (e: Exception) {
                runOnUiThread { Toast.makeText(this, "خطا: ${e.message}", Toast.LENGTH_LONG).show() }
            }
        }.start()
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (web.canGoBack()) web.goBack() else moveTaskToBack(true)
    }
}
