package com.carevoice.app

import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import android.os.Bundle
import android.view.View
import android.webkit.JavascriptInterface
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import com.carevoice.app.databinding.ActivityAdminBinding

/**
 * Admin console = the website's admin dashboard ({server}/admin), shown in a
 * WebView so the app and the website are always identical (Overview, Dispatch,
 * Alerts, Analytics, Approvals, Rooms, Patients, Nurses, Server Logs).
 *
 * Sign-in: the page asks the app for the session token through the
 * "CareVoiceApp" JavaScript bridge, so the admin doesn't log in twice. The
 * bridge only hands the token to pages on the configured server's origin, and
 * navigation off that origin is opened in the external browser instead.
 *
 * Sign out / expired session: the page calls CareVoiceApp.logout() or
 * CareVoiceApp.sessionExpired(), which clear the app session and return to the
 * native login screen.
 */
class AdminActivity : AppCompatActivity() {

    private lateinit var binding: ActivityAdminBinding
    private lateinit var serverOrigin: Uri
    private var leaving = false

    /** Origin of the page currently loading/loaded; read from the JS thread. */
    @Volatile private var pageOrigin: String? = null

    private fun serverUrl(): String {
        val prefs = getSharedPreferences(ServerUploader.PREFS_NAME, Context.MODE_PRIVATE)
        return (prefs.getString(ServerUploader.KEY_SERVER_URL, ServerUploader.DEFAULT_SERVER_URL)
            ?: ServerUploader.DEFAULT_SERVER_URL).trim().trimEnd('/')
    }

    private fun originOf(uri: Uri?): String? =
        uri?.let { u -> u.scheme?.let { s -> "$s://${u.host}${if (u.port > 0) ":${u.port}" else ""}" } }

    private val expectedOrigin: String? get() = originOf(serverOrigin)

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        if (!UserSession.isLoggedIn(this) || UserSession.getRole(this) != "admin") {
            goToLogin(null)
            return
        }

        binding = ActivityAdminBinding.inflate(layoutInflater)
        setContentView(binding.root)
        serverOrigin = Uri.parse(serverUrl())

        binding.webView.apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true          // page uses localStorage
            settings.allowFileAccess = false
            settings.allowContentAccess = false
            settings.setSupportZoom(false)
            settings.useWideViewPort = true
            settings.loadWithOverviewMode = true
            addJavascriptInterface(Bridge(), "CareVoiceApp")
            webViewClient = AdminWebClient()
        }

        binding.btnRetry.setOnClickListener { load() }
        binding.btnLogout.setOnClickListener { goToLogin(null) }

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (binding.webView.canGoBack()) binding.webView.goBack()
                else confirmExit()
            }
        })

        if (savedInstanceState != null) binding.webView.restoreState(savedInstanceState)
        else load()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        if (::binding.isInitialized) binding.webView.saveState(outState)
    }

    override fun onResume() {
        super.onResume()
        if (::binding.isInitialized) binding.webView.onResume()
    }

    override fun onPause() {
        if (::binding.isInitialized) binding.webView.onPause()
        super.onPause()
    }

    override fun onDestroy() {
        if (::binding.isInitialized) {
            binding.webView.removeJavascriptInterface("CareVoiceApp")
            binding.webView.destroy()
        }
        super.onDestroy()
    }

    private fun load() {
        binding.errorView.visibility = View.GONE
        binding.webView.visibility = View.VISIBLE
        binding.webView.loadUrl("${serverUrl()}/admin")
    }

    private fun confirmExit() {
        AlertDialog.Builder(this)
            .setTitle(R.string.admin_title)
            .setMessage(R.string.admin_exit_confirm)
            .setPositiveButton(R.string.admin_exit) { _, _ -> finish() }
            .setNeutralButton(R.string.admin_logout) { _, _ -> goToLogin(null) }
            .setNegativeButton(R.string.settings_cancel, null)
            .show()
    }

    /** Clear the app session (and the page's storage) and return to login. */
    private fun goToLogin(message: Int?) {
        if (leaving) return
        leaving = true
        UserSession.logout(this)
        if (::binding.isInitialized) {
            binding.webView.evaluateJavascript("try{localStorage.clear()}catch(e){}", null)
            binding.webView.clearCache(false)
        }
        message?.let { Toast.makeText(this, it, Toast.LENGTH_LONG).show() }
        startActivity(Intent(this, LoginActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK
        })
        finish()
    }

    // ── JavaScript bridge (called on a background thread) ────────────────────

    private inner class Bridge {
        /** Hand the session token only to pages on our own server. */
        @JavascriptInterface
        fun getToken(): String? =
            if (pageOrigin != null && pageOrigin == expectedOrigin) UserSession.getToken(this@AdminActivity)
            else null

        @JavascriptInterface
        fun logout() { runOnUiThread { goToLogin(null) } }

        @JavascriptInterface
        fun sessionExpired() { runOnUiThread { goToLogin(R.string.admin_session_expired) } }
    }

    // ── WebView client ───────────────────────────────────────────────────────

    private inner class AdminWebClient : WebViewClient() {

        override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
            val url = request.url
            val sameOrigin = originOf(url) == expectedOrigin
            if (!sameOrigin) {
                // Never load other sites inside the console (they'd see the bridge).
                runCatching { startActivity(Intent(Intent.ACTION_VIEW, url)) }
                return true
            }
            // The website's own login page means the session is gone.
            if (url.path == "/login" || url.path == "/") {
                goToLogin(R.string.admin_session_expired)
                return true
            }
            return false
        }

        override fun onPageStarted(view: WebView, url: String?, favicon: Bitmap?) {
            pageOrigin = originOf(url?.let(Uri::parse))
            binding.progress.visibility = View.VISIBLE
        }

        override fun onPageFinished(view: WebView, url: String?) {
            binding.progress.visibility = View.GONE
        }

        override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
            if (!request.isForMainFrame) return
            binding.progress.visibility = View.GONE
            binding.webView.visibility = View.GONE
            binding.tvError.text = getString(R.string.admin_offline_detail, serverUrl(), error.description)
            binding.errorView.visibility = View.VISIBLE
        }
    }
}
