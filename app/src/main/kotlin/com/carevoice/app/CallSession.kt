package com.carevoice.app

import android.content.Context
import android.content.Intent
import android.os.Handler
import android.os.Looper
import android.util.Log
import org.json.JSONObject
import org.webrtc.IceCandidate
import java.util.UUID

/**
 * CallSession — process-wide coordinator that ties together the persistent
 * [SignalingClient], the per-call [WebRtcCallManager], and the call UI.
 *
 * It owns the call state machine and is the single source of truth for "are we
 * in a call and with whom". Both the patient app (MainActivity) and the nurse
 * app (NurseActivity) install ONE signaling client here on login; incoming
 * invites launch [IncomingCallActivity], and active calls are shown by
 * [CallActivity]. This avoids duplicating signaling logic in each activity.
 *
 * Independence: this module has zero dependency on ASR/NLP/TTS/alert code, so
 * calling keeps working even when those subsystems are down.
 */
object CallSession {

    private const val TAG = "CallSession"
    private const val RING_TIMEOUT_MS = 30_000L

    enum class State { IDLE, OUTGOING, INCOMING, CONNECTING, CONNECTED }

    // ── Public observable state (read on main thread) ──────────────────────────
    @Volatile var state: State = State.IDLE; private set
    @Volatile var peerUser: String? = null; private set
    @Volatile var peerName: String? = null; private set
    @Volatile var callId: String? = null; private set
    @Volatile var isCaller: Boolean = false; private set
    @Volatile var roomId: String? = null; private set
    @Volatile var callStartedAt: Long = 0L; private set

    private val main = Handler(Looper.getMainLooper())
    private var appContext: Context? = null
    private var signaling: SignalingClient? = null
    private var rtc: WebRtcCallManager? = null

    /** UI listener — CallActivity registers to reflect state changes. */
    interface UiListener {
        fun onStateChanged(state: State) {}
        fun onCallEnded(reason: String?) {}
    }
    @Volatile private var ui: UiListener? = null
    fun setUiListener(l: UiListener?) { ui = l }
    /** Clear the listener only if [l] is the currently-registered one. */
    fun clearUiListener(l: UiListener?) { if (ui === l) ui = null }

    // ── Signaling lifecycle (called from the host activity on login) ───────────

    @Volatile private var signalingToken: String? = null
    @Volatile private var signalingUrl: String? = null

    fun ensureSignaling(context: Context, serverUrl: String, token: String) {
        appContext = context.applicationContext
        val cleanUrl = serverUrl.trim().trimEnd('/')
        val cleanTok = token.trim()

        // If the token or server changed (e.g. the user logged out and back in,
        // or a new login issued a fresh JWT), the old signaling client is using
        // a STALE token and every reconnect is rejected as "invalid token".
        // Tear it down and create a fresh one bound to the new token.
        val tokenChanged = signalingToken != cleanTok || signalingUrl != cleanUrl
        if (tokenChanged) {
            try { signaling?.close() } catch (_: Exception) {}
            signaling = null
        }
        if (signaling?.isConnected == true) return

        signalingToken = cleanTok
        signalingUrl = cleanUrl
        signaling = SignalingClient(cleanUrl, cleanTok, signalListener).also { it.connect() }
    }

    fun shutdownSignaling() {
        endCall("shutdown")
        signaling?.close()
        signaling = null
        signalingToken = null
        signalingUrl = null
    }

    // ── Placing / answering calls ──────────────────────────────────────────────

    /**
     * Start an outgoing call. `to` may be null for patients (server routes to
     * their attending nurse). `displayName` is what we show while ringing.
     */
    fun placeCall(to: String?, displayName: String, roomId: String? = null) {
        if (state != State.IDLE) { Log.w(TAG, "placeCall ignored, state=$state"); return }
        val id = UUID.randomUUID().toString()
        callId = id; peerUser = to; peerName = displayName; isCaller = true
        this.roomId = roomId; state = State.OUTGOING
        signaling?.sendInvite(id, to, roomId)
        startRingTimeout()
        launchCallUi()
        notifyState()
    }

    /** Accept the current incoming call and begin WebRTC negotiation. */
    fun acceptIncoming() {
        val id = callId ?: return
        val peer = peerUser ?: return
        cancelRingTimeout()
        state = State.CONNECTING
        signaling?.sendAccept(id, peer)
        startRtc(createOffer = false)   // callee waits for the offer
        launchCallUi()
        notifyState()
    }

    fun rejectIncoming(reason: String = "declined") {
        val id = callId ?: return
        val peer = peerUser ?: return
        signaling?.sendReject(id, peer, reason)
        resetState()
        ui?.onCallEnded(reason)
    }

    /** End an active or ringing call from our side. */
    fun endCall(reason: String = "hangup") {
        val id = callId
        val peer = peerUser
        if (peer != null) {
            if (isCaller && state == State.OUTGOING) signaling?.sendCancel(id ?: "", peer, reason)
            else signaling?.sendHangup(id, peer)
        }
        teardownRtc()
        resetState()
        stopCallService()
        ui?.onCallEnded(reason)
    }

    val elapsedSeconds: Long
        get() = if (callStartedAt == 0L) 0 else (System.currentTimeMillis() - callStartedAt) / 1000

    // ── WebRTC wiring ──────────────────────────────────────────────────────────

    private fun startRtc(createOffer: Boolean) {
        val ctx = appContext ?: return
        startCallService()
        rtc = WebRtcCallManager(ctx, object : WebRtcCallManager.Events {
            override fun onLocalIceCandidate(candidate: IceCandidate) {
                val peer = peerUser ?: return
                signaling?.sendIce(callId ?: return, peer, WebRtcCallManager.iceToJson(candidate))
            }
            override fun onConnected() { runMain {
                if (state != State.CONNECTED) {
                    state = State.CONNECTED
                    callStartedAt = System.currentTimeMillis()
                    notifyState()
                }
            } }
            override fun onDisconnected() { runMain { endCall("connection_lost") } }
        }).also { it.start() }

        if (createOffer) {
            rtc?.createOffer { desc ->
                val peer = peerUser ?: return@createOffer
                signaling?.sendOffer(callId ?: return@createOffer, peer, WebRtcCallManager.sdpToJson(desc))
            }
        }
    }

    private fun teardownRtc() {
        try { rtc?.release() } catch (_: Exception) {}
        rtc = null
    }

    fun setMuted(muted: Boolean) = rtc?.setMuted(muted)
    fun setSpeaker(on: Boolean) = rtc?.setSpeakerphone(on)

    // ── Signaling callbacks ─────────────────────────────────────────────────---

    private val signalListener = object : SignalingClient.Listener {
        override fun onConnected() { Log.i(TAG, "Signaling ready") }
        override fun onDisconnected() { Log.w(TAG, "Signaling down") }

        override fun onCallInvite(callId: String, fromUser: String, fromName: String, fromRole: String, roomId: String?) { runMain {
            if (state != State.IDLE) {
                // Already busy — auto-reject so the caller isn't left ringing.
                signaling?.sendReject(callId, fromUser, "busy")
                return@runMain
            }
            this@CallSession.callId = callId; peerUser = fromUser; peerName = fromName
            this@CallSession.roomId = roomId; isCaller = false; state = State.INCOMING
            startRingTimeout()
            launchIncomingUi()
            notifyState()
        } }

        override fun onCallAccept(callId: String, fromUser: String) { runMain {
            if (callId != this@CallSession.callId) return@runMain
            // CRITICAL: when a patient dialled with to=null, peerUser is still
            // null here. Capture the accepter's username so the SDP offer + ICE
            // candidates we send next are addressed to the right peer. Without
            // this, patient→nurse calls connect signaling but never negotiate
            // media (nurse→patient worked only because the nurse knew the peer).
            if (peerUser.isNullOrEmpty()) peerUser = fromUser
            if (peerName.isNullOrEmpty()) peerName = fromUser
            cancelRingTimeout()
            state = State.CONNECTING
            startRtc(createOffer = true)   // caller sends the offer once accepted
            notifyState()
        } }

        override fun onCallReject(callId: String, fromUser: String, reason: String?) { runMain {
            if (callId != this@CallSession.callId) return@runMain
            teardownRtc(); resetState(); stopCallService(); ui?.onCallEnded(reason ?: "rejected")
        } }

        override fun onCallCancel(callId: String, fromUser: String, reason: String?) { runMain {
            if (callId != this@CallSession.callId) return@runMain
            teardownRtc(); resetState(); stopCallService(); ui?.onCallEnded(reason ?: "cancelled")
        } }

        override fun onCallHangup(callId: String?, fromUser: String, reason: String?) { runMain {
            teardownRtc(); resetState(); stopCallService(); ui?.onCallEnded(reason ?: "ended")
        } }

        override fun onSdpOffer(callId: String, fromUser: String, sdp: JSONObject) { runMain {
            rtc?.handleRemoteOffer(WebRtcCallManager.jsonToSdp(sdp)) { desc ->
                signaling?.sendAnswer(callId, fromUser, WebRtcCallManager.sdpToJson(desc))
            }
        } }

        override fun onSdpAnswer(callId: String, fromUser: String, sdp: JSONObject) { runMain {
            rtc?.handleRemoteAnswer(WebRtcCallManager.jsonToSdp(sdp))
        } }

        override fun onIceCandidate(callId: String, fromUser: String, candidate: JSONObject) { runMain {
            rtc?.addRemoteIceCandidate(WebRtcCallManager.jsonToIce(candidate))
        } }

        override fun onBusy(callId: String?, target: String?) { runMain {
            resetState(); ui?.onCallEnded("busy")
        } }
        override fun onPeerOffline(callId: String?, target: String?) { runMain {
            resetState(); ui?.onCallEnded("offline")
        } }
        override fun onError(reason: String?) { runMain {
            if (state == State.OUTGOING) { resetState(); ui?.onCallEnded(reason ?: "error") }
        } }
    }

    /** Run [block] on the main thread. Returns Unit (unlike Handler.post). */
    private fun runMain(block: () -> Unit) { main.post(block) }

    // ── Ring timeout ───────────────────────────────────────────────────────────

    private val ringTimeoutRunnable = Runnable {
        if (state == State.OUTGOING || state == State.INCOMING) {
            endCall("timeout")
        }
    }
    private fun startRingTimeout() { main.postDelayed(ringTimeoutRunnable, RING_TIMEOUT_MS) }
    private fun cancelRingTimeout() { main.removeCallbacks(ringTimeoutRunnable) }

    // ── State helpers + UI launching ───────────────────────────────────────────

    private fun resetState() {
        cancelRingTimeout()
        state = State.IDLE; peerUser = null; peerName = null; callId = null
        isCaller = false; roomId = null; callStartedAt = 0L
    }

    private fun notifyState() { ui?.onStateChanged(state) }

    private fun launchCallUi() {
        val ctx = appContext ?: return
        ctx.startActivity(Intent(ctx, CallActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        })
    }

    private fun launchIncomingUi() {
        val ctx = appContext ?: return
        ctx.startActivity(Intent(ctx, IncomingCallActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        })
    }

    private fun startCallService() {
        val ctx = appContext ?: return
        val i = Intent(ctx, CallService::class.java)
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.O) ctx.startForegroundService(i)
        else ctx.startService(i)
    }
    private fun stopCallService() {
        val ctx = appContext ?: return
        ctx.stopService(Intent(ctx, CallService::class.java))
    }
}
