package com.carevoice.app

import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * SignalingClient — thin OkHttp WebSocket client for the CareVoice call
 * signaling plane (`/ws/signal`).
 *
 * This carries ONLY control messages (SDP offer/answer, ICE candidates, call
 * lifecycle events). It never carries audio — media flows peer-to-peer via
 * [WebRtcCallManager]. The message shape mirrors the server's `SignalMessage`
 * schema: a JSON object with an `event` discriminator.
 *
 * Follows the same OkHttp WebSocket pattern already used by NurseActivity:
 * a dedicated client with a 0-second read timeout (long-lived socket), JSON
 * `event` parsing, plain-text ping/pong keepalive, and 5s reconnect on drop.
 *
 * All callbacks are invoked on OkHttp's WebSocket thread; callers that touch UI
 * must post to the main thread themselves.
 *
 * @param serverUrl  Base HTTP(S) server URL, e.g. "http://192.168.0.103:8000".
 * @param token      JWT access token for the logged-in user.
 */
class SignalingClient(
    private val serverUrl: String,
    private val token: String,
    private val listener: Listener,
) {

    /** Signaling event callbacks. `msg` is the raw relayed JSON object. */
    interface Listener {
        fun onConnected() {}
        fun onDisconnected() {}
        /** A peer wants to start a call with us. */
        fun onCallInvite(callId: String, fromUser: String, fromName: String, fromRole: String, roomId: String?) {}
        /** Our outgoing invite was accepted — we may send the SDP offer now. */
        fun onCallAccept(callId: String, fromUser: String) {}
        fun onCallReject(callId: String, fromUser: String, reason: String?) {}
        fun onCallCancel(callId: String, fromUser: String, reason: String?) {}
        fun onCallHangup(callId: String?, fromUser: String, reason: String?) {}
        fun onSdpOffer(callId: String, fromUser: String, sdp: JSONObject) {}
        fun onSdpAnswer(callId: String, fromUser: String, sdp: JSONObject) {}
        fun onIceCandidate(callId: String, fromUser: String, candidate: JSONObject) {}
        /** Server says the callee is already in another call. */
        fun onBusy(callId: String?, target: String?) {}
        /** Server says the target is not currently connected. */
        fun onPeerOffline(callId: String?, target: String?) {}
        fun onError(reason: String?) {}
    }

    private val TAG = "SignalingClient"

    private val client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.SECONDS)   // long-lived socket
        .pingInterval(20, TimeUnit.SECONDS) // OkHttp-level ping frames
        .build()

    @Volatile private var ws: WebSocket? = null
    @Volatile private var closedByUser = false

    // ── Lifecycle ──────────────────────────────────────────────────────────────

    fun connect() {
        closedByUser = false
        // URL-encode the JWT: it can contain '-'/'_' (base64url, safe) but any
        // stray whitespace/newline or reuse of a mangled value must not corrupt
        // the query string. Trim first, then percent-encode.
        val cleanToken = token.trim()
        val encoded = java.net.URLEncoder.encode(cleanToken, "UTF-8")
        val wsUrl = serverUrl.trim().trimEnd('/')
            .replace("http://", "ws://")
            .replace("https://", "wss://") + "/ws/signal?token=$encoded"

        val req = Request.Builder().url(wsUrl).build()
        ws = client.newWebSocket(req, socketListener)
        Log.d(TAG, "Connecting signaling socket (token_len=${cleanToken.length})")
    }

    fun close() {
        closedByUser = true
        ws?.close(1000, "client closing")
        ws = null
    }

    val isConnected: Boolean get() = ws != null

    // ── Outgoing messages ───────────────────────────────────────────────────---

    /** Start a call. `to` may be null for a patient (server routes to their nurse). */
    fun sendInvite(callId: String, to: String?, roomId: String?) =
        send(JSONObject().apply {
            put("event", "call_invite")
            put("call_id", callId)
            if (to != null) put("to", to)
            if (roomId != null) put("room_id", roomId)
        })

    fun sendAccept(callId: String, to: String) =
        send(JSONObject().apply { put("event", "call_accept"); put("call_id", callId); put("to", to) })

    fun sendReject(callId: String, to: String, reason: String? = null) =
        send(JSONObject().apply {
            put("event", "call_reject"); put("call_id", callId); put("to", to)
            if (reason != null) put("reason", reason)
        })

    fun sendCancel(callId: String, to: String, reason: String? = null) =
        send(JSONObject().apply {
            put("event", "call_cancel"); put("call_id", callId); put("to", to)
            if (reason != null) put("reason", reason)
        })

    fun sendHangup(callId: String?, to: String) =
        send(JSONObject().apply {
            put("event", "call_hangup"); if (callId != null) put("call_id", callId); put("to", to)
        })

    fun sendOffer(callId: String, to: String, sdp: JSONObject) =
        send(JSONObject().apply { put("event", "sdp_offer"); put("call_id", callId); put("to", to); put("sdp", sdp) })

    fun sendAnswer(callId: String, to: String, sdp: JSONObject) =
        send(JSONObject().apply { put("event", "sdp_answer"); put("call_id", callId); put("to", to); put("sdp", sdp) })

    fun sendIce(callId: String, to: String, candidate: JSONObject) =
        send(JSONObject().apply { put("event", "ice_candidate"); put("call_id", callId); put("to", to); put("candidate", candidate) })

    private fun send(obj: JSONObject): Boolean {
        val sock = ws ?: return false
        return sock.send(obj.toString())
    }

    // ── Socket listener ─────────────────────────────────────────────────────---

    private val socketListener = object : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            Log.i(TAG, "Signaling socket open")
            // Server sends its own {"event":"connected"}; onConnected fires on that.
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            if (text == "ping") { webSocket.send("pong"); return }
            if (text == "pong") return

            val j = try { JSONObject(text) } catch (e: Exception) {
                Log.w(TAG, "Bad signaling frame: $text"); return
            }
            val event    = j.optString("event")
            val callId   = j.optString("call_id").ifEmpty { null }
            val fromUser = j.optString("from_user")
            val fromName = j.optString("from_name").ifEmpty { fromUser }
            val fromRole = j.optString("from_role")
            val reason   = j.optString("reason").ifEmpty { null }

            when (event) {
                "connected"     -> listener.onConnected()
                "call_invite"   -> listener.onCallInvite(callId ?: return, fromUser, fromName, fromRole, j.optString("room_id").ifEmpty { null })
                "call_accept"   -> listener.onCallAccept(callId ?: return, fromUser)
                "call_reject"   -> listener.onCallReject(callId ?: return, fromUser, reason)
                "call_cancel"   -> listener.onCallCancel(callId ?: return, fromUser, reason)
                "call_hangup"   -> listener.onCallHangup(callId, fromUser, reason)
                "sdp_offer"     -> listener.onSdpOffer(callId ?: return, fromUser, j.getJSONObject("sdp"))
                "sdp_answer"    -> listener.onSdpAnswer(callId ?: return, fromUser, j.getJSONObject("sdp"))
                "ice_candidate" -> listener.onIceCandidate(callId ?: return, fromUser, j.getJSONObject("candidate"))
                "busy"          -> listener.onBusy(callId, j.optString("to").ifEmpty { null })
                "peer_offline"  -> listener.onPeerOffline(callId, j.optString("to").ifEmpty { null })
                "error"         -> listener.onError(reason)
                else            -> Log.d(TAG, "Unhandled signaling event: $event")
            }
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            Log.w(TAG, "Signaling socket failure: ${t.message}")
            ws = null
            listener.onDisconnected()
            if (!closedByUser) scheduleReconnect()
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            Log.i(TAG, "Signaling socket closed: $code $reason")
            ws = null
            listener.onDisconnected()
            if (!closedByUser) scheduleReconnect()
        }
    }

    private fun scheduleReconnect() {
        Thread {
            try { Thread.sleep(5_000) } catch (_: InterruptedException) { return@Thread }
            if (!closedByUser) {
                Log.i(TAG, "Reconnecting signaling socket…")
                connect()
            }
        }.start()
    }
}
