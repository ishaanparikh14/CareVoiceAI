package com.carevoice.app

import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.carevoice.app.databinding.ActivityCallBinding
import java.util.Locale

/**
 * CallActivity — the in-call screen shown for the duration of an active or
 * outgoing WebRTC voice call. Reflects [CallSession] state (Calling /
 * Connecting / Connected), a running duration timer, and offers Mute, Speaker
 * and End Call controls. All call logic lives in [CallSession]; this screen is
 * a thin view over it.
 */
class CallActivity : AppCompatActivity(), CallSession.UiListener {

    private lateinit var binding: ActivityCallBinding
    private val ticker = Handler(Looper.getMainLooper())
    private var muted = false
    private var speakerOn = false

    private val durationTick = object : Runnable {
        override fun run() {
            if (CallSession.state == CallSession.State.CONNECTED) {
                val s = CallSession.elapsedSeconds
                binding.tvDuration.text = String.format(Locale.US, "%02d:%02d", s / 60, s % 60)
            }
            ticker.postDelayed(this, 1_000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityCallBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.tvPeerName.text = CallSession.peerName ?: CallSession.peerUser ?: "—"

        binding.btnMute.setOnClickListener {
            muted = !muted
            CallSession.setMuted(muted)
            binding.btnMute.text = getString(if (muted) R.string.call_unmute else R.string.call_mute)
            binding.btnMute.setIconResource(if (muted) R.drawable.ic_cv_mic_off else R.drawable.ic_vn_mic)
            highlight(binding.btnMute, muted)
        }
        binding.btnSpeaker.setOnClickListener {
            speakerOn = !speakerOn
            CallSession.setSpeaker(speakerOn)
            highlight(binding.btnSpeaker, speakerOn)
        }
        binding.btnEndCall.setOnClickListener { CallSession.endCall("hangup") }

        onStateChanged(CallSession.state)
    }

    override fun onStart() {
        super.onStart()
        CallSession.setUiListener(this)
        ticker.post(durationTick)
        // If the call already ended before this screen bound, close immediately.
        if (CallSession.state == CallSession.State.IDLE) finish()
    }

    override fun onStop() {
        super.onStop()
        CallSession.clearUiListener(this)
        ticker.removeCallbacks(durationTick)
    }

    override fun onStateChanged(state: CallSession.State) {
        runOnUiThread {
            when (state) {
                CallSession.State.OUTGOING   -> setStatus(R.string.call_status_calling, false)
                CallSession.State.CONNECTING -> setStatus(R.string.call_status_connecting, false)
                CallSession.State.CONNECTED  -> setStatus(R.string.call_status_connected, true)
                CallSession.State.IDLE       -> finish()
                else -> {}
            }
        }
    }

    override fun onCallEnded(reason: String?) {
        runOnUiThread {
            val msg = when (reason) {
                "busy"     -> getString(R.string.call_busy)
                "offline"  -> getString(R.string.call_offline)
                "rejected", "declined" -> getString(R.string.call_rejected)
                "timeout"  -> getString(R.string.call_timeout)
                else        -> getString(R.string.call_ended)
            }
            Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
            finish()
        }
    }

    /** Active control tiles turn solid white with dark content; inactive stay translucent. */
    private fun highlight(button: com.google.android.material.button.MaterialButton, active: Boolean) {
        val fg = if (active) getColor(R.color.cv_call_bg_start) else android.graphics.Color.WHITE
        button.backgroundTintList = android.content.res.ColorStateList.valueOf(
            if (active) android.graphics.Color.WHITE else getColor(R.color.cv_white_12)
        )
        button.setTextColor(fg)
        button.iconTint = android.content.res.ColorStateList.valueOf(fg)
    }

    private fun setStatus(res: Int, showDuration: Boolean) {
        binding.tvCallStatus.setText(res)
        binding.tvDuration.visibility = if (showDuration) android.view.View.VISIBLE else android.view.View.INVISIBLE
    }

    override fun onBackPressed() {
        // Don't kill the call on back; just background the screen.
        moveTaskToBack(true)
    }
}
