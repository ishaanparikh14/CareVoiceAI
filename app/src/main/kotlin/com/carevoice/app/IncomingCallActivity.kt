package com.carevoice.app

import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity
import com.carevoice.app.databinding.ActivityIncomingCallBinding

/**
 * IncomingCallActivity — full-screen incoming-call prompt shown when a
 * `call_invite` arrives. Displays the caller's identity (and room, if a
 * patient) and offers Accept / Decline. Launched by [CallSession] over the lock
 * screen so a call is never missed.
 */
class IncomingCallActivity : AppCompatActivity(), CallSession.UiListener {

    private lateinit var binding: ActivityIncomingCallBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityIncomingCallBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.tvCallerName.text = CallSession.peerName ?: CallSession.peerUser ?: "—"
        val room = CallSession.roomId
        binding.tvCallerRoom.text = if (!room.isNullOrBlank()) "Room $room" else ""

        binding.btnAccept.setOnClickListener {
            CallSession.acceptIncoming()
            // CallActivity is launched by CallSession; close this prompt.
            finish()
        }
        binding.btnReject.setOnClickListener {
            CallSession.rejectIncoming("declined")
            finish()
        }
    }

    override fun onStart() {
        super.onStart()
        CallSession.setUiListener(this)
        // If the invite was cancelled/timed out before we bound, close.
        if (CallSession.state != CallSession.State.INCOMING) finish()
    }

    override fun onStop() {
        super.onStop()
        CallSession.clearUiListener(this)
    }

    override fun onStateChanged(state: CallSession.State) {
        // Once we move past INCOMING (accepted → CONNECTING), close this prompt.
        if (state != CallSession.State.INCOMING) runOnUiThread { finish() }
    }

    override fun onCallEnded(reason: String?) {
        runOnUiThread { finish() }
    }

    override fun onBackPressed() {
        // Treat back as decline.
        CallSession.rejectIncoming("declined")
        finish()
    }
}
