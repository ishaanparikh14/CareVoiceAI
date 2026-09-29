package com.carevoice.app

import android.content.Context
import android.media.AudioManager
import android.util.Log
import org.json.JSONObject
import org.webrtc.AudioSource
import org.webrtc.AudioTrack
import org.webrtc.IceCandidate
import org.webrtc.MediaConstraints
import org.webrtc.MediaStreamTrack
import org.webrtc.PeerConnection
import org.webrtc.PeerConnectionFactory
import org.webrtc.RtpTransceiver
import org.webrtc.SdpObserver
import org.webrtc.SessionDescription
import org.webrtc.audio.JavaAudioDeviceModule

/**
 * WebRtcCallManager — one audio-only, full-duplex WebRTC [PeerConnection] per
 * call. Signaling (SDP + ICE) is delegated to the caller via [Events]; this
 * class owns only the media/transport.
 *
 * Correctness notes (this is a clean rewrite to guarantee two-way audio):
 *  • EXACTLY ONE audio m-line. We add the local mic track with an explicit
 *    SEND_RECV transceiver direction and never call addTransceiver() as well —
 *    adding both produced two m-lines and broke bidirectional audio.
 *  • Audio only. No video encoder/decoder factories are created at all.
 *  • Remote ICE candidates are buffered until the remote description is applied
 *    (WebRTC silently drops early candidates → intermittent no-audio).
 *  • The incoming remote audio track is explicitly enabled on onTrack/onAddTrack
 *    so playback always starts.
 *  • STUN + TURN configured so media flows across NATs / different networks.
 *
 * Threading: WebRTC callbacks arrive on internal WebRTC threads. [Events] are
 * forwarded as-is; the UI layer must marshal to the main thread.
 */
class WebRtcCallManager(
    private val appContext: Context,
    private val events: Events,
) {

    interface Events {
        fun onLocalIceCandidate(candidate: IceCandidate)
        fun onConnected()
        fun onDisconnected()
    }

    private val TAG = "WebRtcCallManager"

    private var factory: PeerConnectionFactory? = null
    private var peerConnection: PeerConnection? = null
    private var audioSource: AudioSource? = null
    private var localAudioTrack: AudioTrack? = null

    // Remote ICE candidates that arrived before the remote description was set.
    private val pendingIce = ArrayList<IceCandidate>()
    @Volatile private var remoteDescriptionSet = false

    private val audioManager = appContext.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private var savedAudioMode = AudioManager.MODE_NORMAL
    private var savedSpeakerOn = false

    // ── Factory ─────────────────────────────────────────────────────────────--

    private fun ensureFactory() {
        if (factory != null) return
        PeerConnectionFactory.initialize(
            PeerConnectionFactory.InitializationOptions.builder(appContext)
                .createInitializationOptions()
        )
        val adm = JavaAudioDeviceModule.builder(appContext)
            .setUseHardwareAcousticEchoCanceler(true)
            .setUseHardwareNoiseSuppressor(true)
            .createAudioDeviceModule()
        // Audio-only: no video factories.
        factory = PeerConnectionFactory.builder()
            .setAudioDeviceModule(adm)
            .createPeerConnectionFactory()
    }

    // ── Start ───────────────────────────────────────────────────────────────--

    fun start() {
        ensureFactory()
        configureAudioForCall()

        val iceServers = listOf(
            PeerConnection.IceServer.builder("stun:stun.l.google.com:19302").createIceServer(),
            PeerConnection.IceServer.builder("stun:stun1.l.google.com:19302").createIceServer(),
            PeerConnection.IceServer.builder("turn:openrelay.metered.ca:80")
                .setUsername("openrelayproject").setPassword("openrelayproject").createIceServer(),
            PeerConnection.IceServer.builder("turn:openrelay.metered.ca:443")
                .setUsername("openrelayproject").setPassword("openrelayproject").createIceServer(),
            PeerConnection.IceServer.builder("turn:openrelay.metered.ca:443?transport=tcp")
                .setUsername("openrelayproject").setPassword("openrelayproject").createIceServer(),
        )
        val rtcConfig = PeerConnection.RTCConfiguration(iceServers).apply {
            sdpSemantics = PeerConnection.SdpSemantics.UNIFIED_PLAN
            continualGatheringPolicy = PeerConnection.ContinualGatheringPolicy.GATHER_CONTINUALLY
            bundlePolicy = PeerConnection.BundlePolicy.MAXBUNDLE
            rtcpMuxPolicy = PeerConnection.RtcpMuxPolicy.REQUIRE
        }

        peerConnection = factory!!.createPeerConnection(rtcConfig, pcObserver)

        // Local mic track (AEC/NS/AGC/highpass requested).
        val audioConstraints = MediaConstraints().apply {
            mandatory.add(MediaConstraints.KeyValuePair("googEchoCancellation", "true"))
            mandatory.add(MediaConstraints.KeyValuePair("googNoiseSuppression", "true"))
            mandatory.add(MediaConstraints.KeyValuePair("googAutoGainControl", "true"))
            mandatory.add(MediaConstraints.KeyValuePair("googHighpassFilter", "true"))
        }
        audioSource = factory!!.createAudioSource(audioConstraints)
        localAudioTrack = factory!!.createAudioTrack("audio0", audioSource).apply { setEnabled(true) }

        // EXACTLY ONE audio m-line: add the track, then force the transceiver
        // direction to SEND_RECV so both sides always send + receive.
        peerConnection?.addTrack(localAudioTrack, listOf("stream0"))
        peerConnection?.transceivers?.firstOrNull {
            it.mediaType == MediaStreamTrack.MediaType.MEDIA_TYPE_AUDIO
        }?.direction = RtpTransceiver.RtpTransceiverDirection.SEND_RECV
    }

    // ── Offer / answer ────────────────────────────────────────────────────────

    fun createOffer(onLocalSdp: (SessionDescription) -> Unit) {
        val pc = peerConnection ?: return
        pc.createOffer(object : SimpleSdpObserver("createOffer") {
            override fun onCreateSuccess(desc: SessionDescription) {
                pc.setLocalDescription(SimpleSdpObserver("setLocal(offer)"), desc)
                onLocalSdp(desc)
            }
        }, MediaConstraints())
    }

    fun handleRemoteOffer(sdp: SessionDescription, onLocalSdp: (SessionDescription) -> Unit) {
        val pc = peerConnection ?: return
        pc.setRemoteDescription(object : SimpleSdpObserver("setRemote(offer)") {
            override fun onSetSuccess() {
                remoteDescriptionSet = true
                drainPendingIceCandidates()
                pc.createAnswer(object : SimpleSdpObserver("createAnswer") {
                    override fun onCreateSuccess(desc: SessionDescription) {
                        pc.setLocalDescription(SimpleSdpObserver("setLocal(answer)"), desc)
                        onLocalSdp(desc)
                    }
                }, MediaConstraints())
            }
        }, sdp)
    }

    fun handleRemoteAnswer(sdp: SessionDescription) {
        val pc = peerConnection ?: return
        pc.setRemoteDescription(object : SimpleSdpObserver("setRemote(answer)") {
            override fun onSetSuccess() {
                remoteDescriptionSet = true
                drainPendingIceCandidates()
            }
        }, sdp)
    }

    fun addRemoteIceCandidate(candidate: IceCandidate) {
        val pc = peerConnection ?: return
        synchronized(pendingIce) {
            if (!remoteDescriptionSet) { pendingIce.add(candidate); return }
        }
        pc.addIceCandidate(candidate)
    }

    private fun drainPendingIceCandidates() {
        val pc = peerConnection ?: return
        val toAdd: List<IceCandidate>
        synchronized(pendingIce) { toAdd = ArrayList(pendingIce); pendingIce.clear() }
        for (c in toAdd) pc.addIceCandidate(c)
    }

    // ── In-call controls ──────────────────────────────────────────────────────

    fun setMuted(muted: Boolean) { localAudioTrack?.setEnabled(!muted) }

    fun setSpeakerphone(on: Boolean) {
        @Suppress("DEPRECATION")
        audioManager.isSpeakerphoneOn = on
    }

    // ── Teardown ──────────────────────────────────────────────────────────────

    fun release() {
        try { peerConnection?.dispose() } catch (_: Exception) {}
        peerConnection = null
        try { localAudioTrack?.dispose() } catch (_: Exception) {}
        localAudioTrack = null
        try { audioSource?.dispose() } catch (_: Exception) {}
        audioSource = null
        synchronized(pendingIce) { pendingIce.clear() }
        remoteDescriptionSet = false
        restoreAudio()
    }

    // ── Audio mode ────────────────────────────────────────────────────────────

    private fun configureAudioForCall() {
        savedAudioMode = audioManager.mode
        @Suppress("DEPRECATION")
        savedSpeakerOn = audioManager.isSpeakerphoneOn
        audioManager.mode = AudioManager.MODE_IN_COMMUNICATION
        @Suppress("DEPRECATION")
        audioManager.isSpeakerphoneOn = true
    }

    private fun restoreAudio() {
        try {
            audioManager.mode = savedAudioMode
            @Suppress("DEPRECATION")
            audioManager.isSpeakerphoneOn = savedSpeakerOn
        } catch (_: Exception) {}
    }

    // ── PeerConnection observer ─────────────────────────────────────────────---

    private val pcObserver = object : PeerConnection.Observer {
        override fun onIceCandidate(candidate: IceCandidate) {
            events.onLocalIceCandidate(candidate)
        }

        override fun onConnectionChange(newState: PeerConnection.PeerConnectionState) {
            Log.i(TAG, "PC state: $newState")
            when (newState) {
                PeerConnection.PeerConnectionState.CONNECTED -> events.onConnected()
                PeerConnection.PeerConnectionState.FAILED,
                PeerConnection.PeerConnectionState.CLOSED -> events.onDisconnected()
                else -> {}
            }
        }

        override fun onIceConnectionChange(newState: PeerConnection.IceConnectionState) {
            Log.i(TAG, "ICE state: $newState")
            if (newState == PeerConnection.IceConnectionState.FAILED) events.onDisconnected()
        }

        // Ensure the incoming remote audio track is enabled for playback.
        override fun onAddTrack(receiver: org.webrtc.RtpReceiver?, streams: Array<out org.webrtc.MediaStream>?) {
            (receiver?.track() as? AudioTrack)?.setEnabled(true)
            Log.i(TAG, "Remote track added — audio enabled")
        }

        override fun onTrack(transceiver: RtpTransceiver?) {
            (transceiver?.receiver?.track() as? AudioTrack)?.setEnabled(true)
            Log.i(TAG, "onTrack — remote audio enabled")
        }

        override fun onSignalingChange(p0: PeerConnection.SignalingState?) {}
        override fun onIceConnectionReceivingChange(p0: Boolean) {}
        override fun onIceGatheringChange(p0: PeerConnection.IceGatheringState?) {}
        override fun onIceCandidatesRemoved(p0: Array<out IceCandidate>?) {}
        override fun onAddStream(p0: org.webrtc.MediaStream?) {}
        override fun onRemoveStream(p0: org.webrtc.MediaStream?) {}
        override fun onDataChannel(p0: org.webrtc.DataChannel?) {}
        override fun onRenegotiationNeeded() {}
    }

    // ── JSON <-> WebRTC conversion ────────────────────────────────────────────

    companion object {
        fun sdpToJson(desc: SessionDescription): JSONObject = JSONObject().apply {
            put("type", desc.type.canonicalForm())
            put("sdp", desc.description)
        }

        fun jsonToSdp(json: JSONObject): SessionDescription {
            val type = SessionDescription.Type.fromCanonicalForm(json.getString("type"))
            return SessionDescription(type, json.getString("sdp"))
        }

        fun iceToJson(c: IceCandidate): JSONObject = JSONObject().apply {
            put("candidate", c.sdp)
            put("sdpMid", c.sdpMid)
            put("sdpMLineIndex", c.sdpMLineIndex)
        }

        fun jsonToIce(json: JSONObject): IceCandidate = IceCandidate(
            json.optString("sdpMid"),
            json.optInt("sdpMLineIndex"),
            json.getString("candidate"),
        )
    }
}

/** No-op SDP observer with overridable success hooks + error logging. */
private open class SimpleSdpObserver(private val tag: String = "sdp") : SdpObserver {
    override fun onCreateSuccess(desc: SessionDescription) {}
    override fun onSetSuccess() {}
    override fun onCreateFailure(error: String?) { Log.w("WebRtc", "$tag create failed: $error") }
    override fun onSetFailure(error: String?) { Log.w("WebRtc", "$tag set failed: $error") }
}
