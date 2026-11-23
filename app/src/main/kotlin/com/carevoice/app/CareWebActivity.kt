package com.carevoice.app

import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import android.os.Bundle
import android.view.View
import android.Manifest
import android.content.pm.PackageManager
import android.webkit.JavascriptInterface
import android.webkit.PermissionRequest
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.carevoice.app.databinding.ActivityAdminBinding

/**
 * CareWebActivity — a WebView host for the nurse "Patient Care" editor
 * ({server}/nurse-care), where a nurse adjusts diet + medications for the
 * patients she is mapped to. Mirrors AdminActivity's hardened WebView + JS
 * bridge (so the session token is handed only to same-origin pages), but opens
 * /nurse-care and permits the nurse (or admin) role.
 *
 * Reuses the admin layout (ActivityAdminBinding) to avoid a new XML file.
 */
class CareWebActivity : AppCompatActivity() {

    private lateinit var binding: ActivityAdminBinding
    private lateinit var serverOrigin: Uri
    private var leaving = false

    @Volatile private var pageOrigin: String? = null

    private var pendingMicRequest: PermissionRequest? = null
    private val micPermission =
        registerForActivityResult(androidx.activity.result.contract.ActivityResultContracts.RequestPermission()) { ok ->
            val req = pendingMicRequest
            pendingMicRequest = null
            if (ok) req?.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE)) else req?.deny()
        }

    private val askMicPermission =
        registerForActivityResult(androidx.activity.result.contract.ActivityResultContracts.RequestPermission()) { ok ->
            if (ok) beginAskRecording() else js("window.cvOnRecState&&cvOnRecState('denied')")
        }

    private val askRecorder by lazy {
        VoiceNoteRecorder(this).apply {
            setOnFinished { wav, ms ->
                if (wav.size < 2_000) {
                    // Recorder could not open the mic (busy) or nothing captured.
                    js("window.cvOnRecState&&cvOnRecState('error')")
                } else {
                    val b64 = android.util.Base64.encodeToString(wav, android.util.Base64.NO_WRAP)
                    js("window.cvOnRecorded&&cvOnRecorded('$b64',$ms)")
                }
            }
        }
    }

    private fun beginAskRecording() {
        if (askRecorder.isRecording()) return
        askRecorder.start(lifecycleScope) { js("window.cvOnRecState&&cvOnRecState('recording')") }
    }

    private fun js(code: String) {
        if (::binding.isInitialized) binding.webView.evaluateJavascript(code, null)
    }

    /** Native read-aloud for AskCareVoice replies (en-IN / hi-IN). */
    private val tts by lazy { TtsManager(this) }
    private var ttsUsed = false

    companion object {
        private const val EXTRA_PATH = "path"

        /** Pages a patient-role session may open (everything else is staff-only). */
        private val PATIENT_PAGES = setOf("/my-health", "/attender")
        private val STAFF_PAGES = setOf("/nurse-care")

        fun open(ctx: Context, path: String = "/nurse-care") {
            ctx.startActivity(Intent(ctx, CareWebActivity::class.java).putExtra(EXTRA_PATH, path))
        }
    }

    private val path: String
        get() = intent.getStringExtra(EXTRA_PATH)?.takeIf { it in PATIENT_PAGES || it in STAFF_PAGES }
            ?: "/nurse-care"

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

        val role = UserSession.getRole(this)
        val allowed = when (path) {
            in PATIENT_PAGES -> role == "patient"
            else -> role == "nurse" || role == "admin"
        }
        if (!UserSession.isLoggedIn(this) || !allowed) {
            if (UserSession.isLoggedIn(this)) { finish(); return }
            goToLogin()
            return
        }

        binding = ActivityAdminBinding.inflate(layoutInflater)
        setContentView(binding.root)
        serverOrigin = Uri.parse(serverUrl())
        // Warm up text-to-speech now so the first AskCareVoice answer is spoken
        // (the engine takes a moment to initialise).
        if (path == "/my-health") { ttsUsed = true; tts }

        binding.webView.apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.allowFileAccess = false
            settings.allowContentAccess = false
            settings.setSupportZoom(false)
            settings.useWideViewPort = true
            settings.loadWithOverviewMode = true
            addJavascriptInterface(Bridge(), "CareVoiceApp")
            webViewClient = CareWebClient()
            // AskCareVoice hold-to-talk: grant the microphone to OUR OWN pages
            // only (same origin), and only if the app holds RECORD_AUDIO.
            settings.mediaPlaybackRequiresUserGesture = false
            webChromeClient = object : WebChromeClient() {
                override fun onPermissionRequest(request: PermissionRequest) {
                    runOnUiThread {
                        val sameOrigin = originOf(request.origin) == expectedOrigin
                        val wantsMic = request.resources.contains(PermissionRequest.RESOURCE_AUDIO_CAPTURE)
                        if (!sameOrigin || !wantsMic) { request.deny(); return@runOnUiThread }
                        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                            request.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
                        } else {
                            pendingMicRequest = request
                            micPermission.launch(Manifest.permission.RECORD_AUDIO)
                        }
                    }
                }
            }
        }

        binding.btnRetry.setOnClickListener { load() }
        binding.btnLogout.setOnClickListener { finish() }

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (binding.webView.canGoBack()) binding.webView.goBack() else finish()
            }
        })

        if (savedInstanceState != null) binding.webView.restoreState(savedInstanceState)
        else load()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        if (::binding.isInitialized) binding.webView.saveState(outState)
    }

    override fun onResume() { super.onResume(); if (::binding.isInitialized) binding.webView.onResume() }
    override fun onPause() {
        // Leaving the screen mid-question: release the mic (what was said so far
        // is still sent) and stop any speech.
        if (askRecorder.isRecording()) askRecorder.stop()
        if (ttsUsed) tts.stop()
        if (::binding.isInitialized) binding.webView.onPause()
        super.onPause()
    }

    override fun onDestroy() {
        if (ttsUsed) tts.shutdown()
        pendingMicRequest?.deny()
        if (::binding.isInitialized) {
            binding.webView.removeJavascriptInterface("CareVoiceApp")
            binding.webView.destroy()
        }
        super.onDestroy()
    }

    private fun load() {
        binding.errorView.visibility = View.GONE
        binding.webView.visibility = View.VISIBLE
        binding.webView.loadUrl("${serverUrl()}$path")
    }

    private fun goToLogin() {
        if (leaving) return
        leaving = true
        startActivity(Intent(this, LoginActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK
        })
        finish()
    }

    private inner class Bridge {
        @JavascriptInterface
        fun getToken(): String? =
            if (pageOrigin != null && pageOrigin == expectedOrigin) UserSession.getToken(this@CareWebActivity)
            else null

        @JavascriptInterface
        fun logout() { runOnUiThread { finish() } }

        @JavascriptInterface
        fun back() { runOnUiThread { finish() } }

        @JavascriptInterface
        fun speak(text: String?, lang: String?) {
            if (pageOrigin == null || pageOrigin != expectedOrigin || text.isNullOrBlank()) return
            runOnUiThread {
                ttsUsed = true
                tts.speakVoiceNote(text.take(1200), lang ?: "en", 0)
            }
        }

        @JavascriptInterface
        fun stopSpeaking() { runOnUiThread { if (ttsUsed) tts.stop() } }

        /** Ask CareVoice: record with the phone's own recorder (no WebView
         *  microphone permission involved). Result -> window.cvOnRecorded. */
        @JavascriptInterface
        fun startRecording() {
            if (pageOrigin == null || pageOrigin != expectedOrigin) return
            runOnUiThread {
                if (ttsUsed) tts.stop()
                if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                    beginAskRecording()
                } else {
                    askMicPermission.launch(Manifest.permission.RECORD_AUDIO)
                }
            }
        }

        @JavascriptInterface
        fun stopRecording() { runOnUiThread { askRecorder.stop() } }

        @JavascriptInterface
        fun sessionExpired() { runOnUiThread { goToLogin() } }
    }

    private inner class CareWebClient : WebViewClient() {
        override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
            val url = request.url
            if (originOf(url) != expectedOrigin) {
                runCatching { startActivity(Intent(Intent.ACTION_VIEW, url)) }
                return true
            }
            if (url.path == "/login" || url.path == "/") { goToLogin(); return true }
            return false
        }

        override fun onPageStarted(view: WebView, url: String?, favicon: Bitmap?) {
            pageOrigin = originOf(url?.let(Uri::parse))
            binding.progress.visibility = View.VISIBLE
        }

        override fun onPageFinished(view: WebView, url: String?) { binding.progress.visibility = View.GONE }

        override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
            if (!request.isForMainFrame) return
            binding.progress.visibility = View.GONE
            binding.webView.visibility = View.GONE
            binding.tvError.text = getString(R.string.admin_offline_detail, serverUrl(), error.description)
            binding.errorView.visibility = View.VISIBLE
        }
    }
}
