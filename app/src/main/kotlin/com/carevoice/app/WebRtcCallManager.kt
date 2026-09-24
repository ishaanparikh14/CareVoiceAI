package com.carevoice.app

import android.content.Context
import android.media.AudioManager
import android.util.Log
import org.json.JSONObject
import org.webrtc.AudioSource
import org.webrtc.AudioTrack
import org.webrtc.DefaultVideoDecoderFactory
import org.webrtc.DefaultVideoEncoderFactory
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
 * WebRtcCallManager — wraps a single audio-only WebRTC [PeerConnection] for one
 * call. Signaling (SDP + ICE) is delegated to the caller via [Events]; this
 * class owns only the media/transport.
 *
 * Design choices dictated by the hospital-LAN requirements:
 *  • Audio only, full duplex. No video tracks are ever created.
 *  • No ICE servers configured → only host candidates are gathered, so media
 *    goes straight device-to-device over the LAN. No STUN/TURN, no internet.
 *  • Opus is WebRTC's default audio codec; low-latency by design.
 *  • Hardware/software AEC, NS and AGC are enabled on the audio device module
 *    and requested again as SDP media constraints.
 *  • Nothing is recorded, written to disk, transcribed or analysed.
 *
 * Threading: WebRTC callbacks arrive on internal WebRTC threads. [Events] are
 * forwarded as-is; the UI layer must marshal to the main thread.
 */
class WebRtcCallManager(
    private val appContext: Context,
    private val events: Events,
) {

    interface Events {
        /** A locally-gathered ICE candidate to send to the peer. */
        fun onLocalIceCandidate(candidate: IceCandidate)
        /** PeerConnection reached CONNECTED — media is flowing. */
        fun onConnected()
        /** Connection failed or dropped (ICE failed/disconnected/closed). */
        fun onDisconnected()
    }

    private val TAG = "WebRtcCallManager"

    private var factory: PeerConnectionFactory? = null
    private var peerConnection: PeerConnection? = null
    private var audioSource: AudioSource? = null
    private var localAudioTrack: AudioTrack? = null

    private val audioManager = appContext.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private var savedAudioMode = AudioManager.MODE_NORMAL
    private var savedSpeakerOn = false

    // ── Init / factory ──────────────────────────────────────────────────────---

    private fun ensureFactory() {
        if (factory != null) return

        PeerConnectionFactory.initialize(
            PeerConnectionFactory.InitializationOptions.builder(appContext)
                .createInitializationOptions()
        )

        // Audio device module with echo cancellation / noise suppression on.
        val adm = JavaAudioDeviceModule.builder(appContext)
            .setUseHardwareAcousticEchoCanceler(true)
            .setUseHardwareNoiseSuppressor(true)
            .createAudioDeviceModule()

        factory = PeerConnectionFactory.builder()
            .setAudioDeviceModule(adm)
            .setVideoEncoderFactory(DefaultVideoEncoderFactory(EglBaseHolder.eglBase.eglBaseContext, true, true))
            .setVideoDecoderFactory(DefaultVideoDecoderFactory(EglBaseHolder.eglBase.eglBaseContext))
            .createPeerConnectionFactory()
    }

    /**
     * Build the peer connection + local mic track. Must be called before
     * [createOffer] / [handleRemoteOffer].
     */
    fun start() {
        ensureFactory()
        configureAudioForCall()

        // No ICE servers → host candidates only → pure LAN peer-to-peer.
        val rtcConfig = PeerConnection.RTCConfiguration(emptyList()).apply {
            sdpSemantics = PeerConnection.SdpSemantics.UNIFIED_PLAN
            // Keep candidate gathering minimal for low latency on a trusted LAN.
            continualGatheringPolicy = PeerConnection.ContinualGatheringPolicy.GATHER_CONTINUALLY
            bundlePolicy = PeerConnection.BundlePolicy.MAXBUNDLE
            rtcpMuxPolicy = PeerConnection.RtcpMuxPolicy.REQUIRE
        }

        peerConnection = factory!!.createPeerConnection(rtcConfig, pcObserver)

        // Local microphone track with AEC/NS/AGC + high-pass filter requested.
        val audioConstraints = MediaConstraints().apply {
            mandatory.add(MediaConstraints.KeyValuePair("googEchoCancellation", "true"))
            mandatory.add(MediaConstraints.KeyValuePair("googNoiseSuppression", "true"))
            mandatory.add(MediaConstraints.KeyValuePair("googAutoGainControl", "true"))
            mandatory.add(MediaConstraints.KeyValuePair("googHighpassFilter", "true"))
        }
        audioSource = factory!!.createAudioSource(audioConstraints)
        localAudioTrack = factory!!.createAudioTrack("audio0", audioSource).apply {
            setEnabled(true)
        }
        // Send + receive audio (full duplex).
        peerConnection?.addTransceiver(
            MediaStreamTrack.MediaType.MEDIA_TYPE_AUDIO,
            RtpTransceiver.RtpTransceiverInit(RtpTransceiver.RtpTransceiverDirection.SEND_RECV)
        )
        peerConnection?.addTrack(localAudioTrack, listOf("stream0"))
    }

    // ── Offer / answer ────────────────────────────────────────────────────────

    /** Caller side: create an SDP offer and hand it back via [onLocalSdp]. */
    fun createOffer(onLocalSdp: (SessionDescription) -> Unit) {
        val pc = peerConnection ?: return
        pc.createOffer(object : SimpleSdpObserver() {
            override fun onCreateSuccess(desc: SessionDescription) {
                pc.setLocalDescription(SimpleSdpObserver(), desc)
                onLocalSdp(desc)
            }
        }, mediaConstraintsRecvAudio())
    }

    /** Callee side: apply the remote offer, then produce the answer. */
    fun handleRemoteOffer(sdp: SessionDescription, onLocalSdp: (SessionDescription) -> Unit) {
        val pc = peerConnection ?: return
        pc.setRemoteDescription(object : SimpleSdpObserver() {
            override fun onSetSuccess() {
                pc.createAnswer(object : SimpleSdpObserver() {
                    override fun onCreateSuccess(desc: SessionDescription) {
                        pc.setLocalDescription(SimpleSdpObserver(), desc)
                        onLocalSdp(desc)
                    }
                }, mediaConstraintsRecvAudio())
            }
        }, sdp)
    }

    /** Caller side: apply the remote answer. */
    fun handleRemoteAnswer(sdp: SessionDescription) {
        peerConnection?.setRemoteDescription(SimpleSdpObserver(), sdp)
    }

    fun addRemoteIceCandidate(candidate: IceCandidate) {
        peerConnection?.addIceCandidate(candidate)
    }

    // ── In-call controls ──────────────────────────────────────────────────────

    /** Mute/unmute the outgoing microphone track. Returns the new muted state. */
    fun setMuted(muted: Boolean) {
        localAudioTrack?.setEnabled(!muted)
    }

    /** Route audio to the loudspeaker (true) or earpiece (false). */
    fun setSpeakerphone(on: Boolean) {
        @Suppress("DEPRECATION")
        audioManager.isSpeakerphoneOn = on
    }

    // ── Teardown ──────────────────────────────────────────────────────────────

    /**
     * Close the peer connection and release ALL audio resources (mic included).
     * Safe to call multiple times.
     */
    fun release() {
        try { peerConnection?.dispose() } catch (_: Exception) {}
        peerConnection = null
        try { localAudioTrack?.dispose() } catch (_: Exception) {}
        localAudioTrack = null
        try { audioSource?.dispose() } catch (_: Exception) {}
        audioSource = null
        restoreAudio()
        // Factory is kept for reuse across calls; dispose on app exit if needed.
    }

    // ── Audio mode helpers ──────────────────────────────────────────────────---

    private fun configureAudioForCall() {
        savedAudioMode = audioManager.mode
        @Suppress("DEPRECATION")
        savedSpeakerOn = audioManager.isSpeakerphoneOn
        audioManager.mode = AudioManager.MODE_IN_COMMUNICATION
    }

    private fun restoreAudio() {
        try {
            audioManager.mode = savedAudioMode
            @Suppress("DEPRECATION")
            audioManager.isSpeakerphoneOn = savedSpeakerOn
        } catch (_: Exception) {}
    }

    private fun mediaConstraintsRecvAudio() = MediaConstraints().apply {
        mandatory.add(MediaConstraints.KeyValuePair("OfferToReceiveAudio", "true"))
        mandatory.add(MediaConstraints.KeyValuePair("OfferToReceiveVideo", "false"))
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
                PeerConnection.PeerConnectionState.DISCONNECTED,
                PeerConnection.PeerConnectionState.CLOSED -> events.onDisconnected()
                else -> {}
            }
        }

        override fun onIceConnectionChange(newState: PeerConnection.IceConnectionState) {
            if (newState == PeerConnection.IceConnectionState.FAILED) events.onDisconnected()
        }

        override fun onSignalingChange(p0: PeerConnection.SignalingState?) {}
        override fun onIceConnectionReceivingChange(p0: Boolean) {}
        override fun onIceGatheringChange(p0: PeerConnection.IceGatheringState?) {}
        override fun onIceCandidatesRemoved(p0: Array<out IceCandidate>?) {}
        override fun onAddStream(p0: org.webrtc.MediaStream?) {}
        override fun onRemoveStream(p0: org.webrtc.MediaStream?) {}
        override fun onDataChannel(p0: org.webrtc.DataChannel?) {}
        override fun onRenegotiationNeeded() {}
        override fun onAddTrack(p0: org.webrtc.RtpReceiver?, p1: Array<out org.webrtc.MediaStream>?) {}
    }

    // ── JSON <-> WebRTC conversion helpers ────────────────────────────────────

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

/** No-op SDP observer with overridable success hooks. */
private open class SimpleSdpObserver : SdpObserver {
    override fun onCreateSuccess(desc: SessionDescription) {}
    override fun onSetSuccess() {}
    override fun onCreateFailure(error: String?) { Log.w("WebRtc", "SDP create failed: $error") }
    override fun onSetFailure(error: String?) { Log.w("WebRtc", "SDP set failed: $error") }
}

/** Lazily-created shared EglBase so the factory can be built once. */
private object EglBaseHolder {
    val eglBase: org.webrtc.EglBase by lazy { org.webrtc.EglBase.create() }
}
