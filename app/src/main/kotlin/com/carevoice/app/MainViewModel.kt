package com.carevoice.app

import android.app.Application
import android.content.Context
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.util.Log
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.LiveData
import androidx.lifecycle.MutableLiveData
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class MainViewModel(application: Application) : AndroidViewModel(application) {

    private val TAG = "MainViewModel"

    // ── UI state ──────────────────────────────────────────────────────────────

    sealed class UiState {
        object Idle              : UiState()
        data class Downloading(val percent: Int) : UiState()
        object ModelReady        : UiState()
        /** Waiting for the "help" / "nurse" wake word. */
        object WakeWordListening : UiState()
        /** Energy-VAD fallback path (SpeechRecognizer unavailable). */
        object Listening         : UiState()
        /** Wake word heard — recording the follow-up message. */
        object Triggered         : UiState()
        /** Manual recording in progress (Stop button shows). */
        object ManualRecording   : UiState()
        object SpeechDetected    : UiState()
        object Sent              : UiState()
        /** The message reached the server but no request was understood. */
        object Ignored           : UiState()
        object DownloadError     : UiState()
        object UploadError       : UiState()
        /** Listening paused because the patient screen is not in front. */
        object Paused            : UiState()
    }

    /** One-shot message for the patient (shown as a banner + status line). */
    data class Feedback(val text: String, val kind: Kind, val id: Long = System.nanoTime()) {
        enum class Kind { INFO, SUCCESS, ERROR }
    }

    // ── LiveData ──────────────────────────────────────────────────────────────

    private val _uiState  = MutableLiveData<UiState>(UiState.Idle)
    val uiState: LiveData<UiState> = _uiState

    private val _rmsLevel = MutableLiveData(0f)
    val rmsLevel: LiveData<Float> = _rmsLevel

    private val _feedback = MutableLiveData<Feedback?>(null)
    val feedback: LiveData<Feedback?> = _feedback

    /** Detail line under the status (what the server said / what was heard). */
    private val _detail = MutableLiveData<String?>(null)
    val detail: LiveData<String?> = _detail

    /** Number of requests waiting to be re-sent. */
    private val _pendingCount = MutableLiveData(0)
    val pendingCount: LiveData<Int> = _pendingCount

    // ── Resources ─────────────────────────────────────────────────────────────

    private var sileroVAD:         SileroVAD?        = null
    private var audioRecorder:     AudioRecorder?    = null
    private var wakeWordDetector:  WakeWordDetector? = null
    private var manualRecordingJob: Job? = null
    private var retryJob: Job? = null
    private var resumeJob: Job? = null
    private var lastHeardHintAt = 0L
    private var paused = false
    private val serverUploader = ServerUploader(application)
    private val pending = PendingRequests(application)

    init {
        WakeWordDetector.restoreIfLeftMuted(application)
        _pendingCount.value = pending.all().size
        if (!pending.isEmpty()) startRetryLoop()
    }

    private fun say(text: String, kind: Feedback.Kind) {
        _feedback.postValue(Feedback(text, kind))
    }

    // ── Model download ────────────────────────────────────────────────────────

    fun ensureModelReady() {
        val current = _uiState.value
        if (current == UiState.ModelReady) return

        viewModelScope.launch(Dispatchers.Main) {
            try {
                if (sileroVAD == null) sileroVAD = SileroVAD()
                _uiState.value = UiState.ModelReady
            } catch (e: Exception) {
                Log.e(TAG, "Resource preparation failed", e)
                _uiState.value = UiState.DownloadError
            }
        }
    }

    // ── Listening control ─────────────────────────────────────────────────────

    /**
     * Start wake-word detection. When "help/nurse/…" is heard the app records
     * the follow-up message and sends it. Falls back to energy-VAD if
     * SpeechRecognizer is unavailable.
     */
    fun startListening() {
        resumeJob?.cancel()
        if (paused) return
        audioRecorder?.stop()
        wakeWordDetector?.stop()
        manualRecordingJob?.cancel()

        val app: Application = getApplication()
        val detector = WakeWordDetector(
            app,
            onHeard = { heard -> onSpeechWithoutWakeWord(heard) },
        ) {
            viewModelScope.launch(Dispatchers.Main) {
                Log.i(TAG, "Wake word fired — starting recording")
                buzz(longArrayOf(0, 60, 80, 60))   // silent "I heard you" cue
                _detail.value = null
                _uiState.value = UiState.Triggered
                wakeWordDetector?.stop()
                startWakeWordRecording()
            }
        }

        if (detector.isAvailable()) {
            wakeWordDetector = detector
            detector.start()
            _uiState.value = UiState.WakeWordListening
        } else {
            Log.w(TAG, "SpeechRecognizer unavailable — falling back to energy VAD")
            say("Voice wake word is not available on this phone. Use the Call Nurse button.",
                Feedback.Kind.ERROR)
            startEnergyVAD()
        }
    }

    /** Speech was recognised but no wake word matched: tell the patient (throttled). */
    private fun onSpeechWithoutWakeWord(heard: String) {
        val now = System.currentTimeMillis()
        if (now - lastHeardHintAt < 6_000) return
        if (heard.length < 2) return
        lastHeardHintAt = now
        _detail.value = "Heard \"${heard.take(40)}\" — say \"Help\" or \"Nurse\" to call"
        viewModelScope.launch {
            delay(6_000)
            if (System.currentTimeMillis() - lastHeardHintAt >= 6_000 &&
                _uiState.value == UiState.WakeWordListening) _detail.value = null
        }
    }

    /** Called when the patient screen goes to the background / returns. */
    fun pause() {
        paused = true
        resumeJob?.cancel()
        if (manualRecordingJob?.isActive == true) return   // let a message in progress finish
        wakeWordDetector?.stop()
        audioRecorder?.stop()
        if (_uiState.value == UiState.WakeWordListening || _uiState.value == UiState.Listening) {
            _uiState.value = UiState.Paused
        }
    }

    fun resume() {
        val wasPaused = paused
        paused = false
        val st = _uiState.value
        val busy = manualRecordingJob?.isActive == true || st == UiState.SpeechDetected ||
            st == UiState.ModelReady || st == UiState.DownloadError || st is UiState.Downloading
        if (wasPaused && !busy) startListening()
    }

    /** Energy-VAD fallback used when SpeechRecognizer is not available. */
    private fun startEnergyVAD() {
        val vad = sileroVAD ?: return
        if (audioRecorder == null) {
            audioRecorder = AudioRecorder(
                context          = getApplication(),
                sileroVAD        = vad,
                scope            = viewModelScope,
                onTrigger        = {
                    viewModelScope.launch(Dispatchers.Main) { _uiState.value = UiState.Triggered }
                },
                onSpeechDetected = ::handleSpeechDetected,
                onRmsUpdate      = { rms ->
                    viewModelScope.launch(Dispatchers.Main) { _rmsLevel.value = rms }
                }
            )
        }
        audioRecorder?.start()
        _uiState.value = UiState.Listening
    }

    private val silenceThreshold: Float
        get() = when (getApplication<Application>()
            .getSharedPreferences(ServerUploader.PREFS_NAME, Context.MODE_PRIVATE)
            .getString(WakeWordDetector.KEY_SENSITIVITY, "normal")) {
            "high" -> 0.010f
            "low"  -> 0.022f
            else   -> 0.014f
        }

    /**
     * Record the message right after the wake word. Stops after ~1.6 s of
     * silence (min 1.5 s) or 10 s max, then uploads.
     */
    private fun startWakeWordRecording() {
        manualRecordingJob?.cancel()
        manualRecordingJob = viewModelScope.launch(Dispatchers.IO) {
            val sampleRate   = 16_000
            val chunkSamples = 512
            val maxSamples   = sampleRate * 10
            val threshold    = silenceThreshold

            val minBuf = android.media.AudioRecord.getMinBufferSize(
                sampleRate,
                android.media.AudioFormat.CHANNEL_IN_MONO,
                android.media.AudioFormat.ENCODING_PCM_16BIT
            )
            val recorder = try {
                android.media.AudioRecord(
                    android.media.MediaRecorder.AudioSource.VOICE_RECOGNITION,
                    sampleRate,
                    android.media.AudioFormat.CHANNEL_IN_MONO,
                    android.media.AudioFormat.ENCODING_PCM_16BIT,
                    minBuf * 4
                )
            } catch (e: Exception) { null }

            if (recorder == null || recorder.state != android.media.AudioRecord.STATE_INITIALIZED) {
                Log.e(TAG, "AudioRecord failed to init in startWakeWordRecording")
                recorder?.release()
                say("Could not use the microphone. Please tap Call Nurse.", Feedback.Kind.ERROR)
                withContext(Dispatchers.Main) {
                    _uiState.value = UiState.UploadError
                    _detail.value = "Microphone busy — tap Call Nurse"
                    resumeAfter(4_000, UiState.UploadError)
                }
                return@launch
            }

            recorder.startRecording()
            withContext(Dispatchers.Main) { _uiState.value = UiState.ManualRecording }

            val allSamples   = mutableListOf<FloatArray>()
            val shortBuf     = ShortArray(chunkSamples)
            var totalSamples = 0
            var silenceCount = 0
            var heardVoice   = false

            try {
                while (isActive && totalSamples < maxSamples) {
                    val read = recorder.read(shortBuf, 0, chunkSamples)
                    if (read < 0) break
                    if (read != chunkSamples) continue

                    val chunk = FloatArray(chunkSamples) { i -> shortBuf[i] / 32768f }
                    val rms   = WavUtils.computeRms(chunk)

                    withContext(Dispatchers.Main) { _rmsLevel.value = rms }
                    allSamples.add(chunk)
                    totalSamples += chunkSamples

                    if (rms >= threshold) heardVoice = true
                    silenceCount = if (rms < threshold) silenceCount + 1 else 0
                    // Patients often pause after "help" — wait longer before the
                    // first word than after speech has started.
                    val needed = if (heardVoice) 50 else 110
                    if (totalSamples > sampleRate * 3 / 2 && silenceCount >= needed) break
                }
            } finally {
                try { recorder.stop(); recorder.release() } catch (_: Exception) {}
            }

            val utterance = allSamples.flatMap { it.toList() }.toFloatArray()
            handleSpeechDetected(utterance)
        }
    }

    private fun resumeAfter(ms: Long, ifState: UiState) {
        resumeJob?.cancel()
        resumeJob = viewModelScope.launch(Dispatchers.Main) {
            delay(ms)
            if (_uiState.value == ifState) startListening()
        }
    }

    // ── Stop / cancel ─────────────────────────────────────────────────────────

    /** Cancel active recording and resume wake-word detection. */
    fun stopListening() {
        if (manualRecordingJob?.isActive == true) {
            manualRecordingJob?.cancel()
            manualRecordingJob = null
            _rmsLevel.value = 0f
            _detail.value = "Recording cancelled"
            startListening()
        } else {
            fullStop()
        }
    }

    /** Full teardown — called on logout or onCleared. */
    fun fullStop() {
        audioRecorder?.stop()
        wakeWordDetector?.stop()
        manualRecordingJob?.cancel()
        resumeJob?.cancel()
        _rmsLevel.value = 0f
        _uiState.value = UiState.Idle
    }

    // ── Instant manual call ───────────────────────────────────────────────────

    /** Send an Emergency alert immediately (Call Nurse button). Retries if offline. */
    fun callNurseNow() {
        val key = PendingRequests.newKey()
        viewModelScope.launch(Dispatchers.IO) {
            withContext(Dispatchers.Main) { _uiState.value = UiState.SpeechDetected }
            val res = serverUploader.sendManualAlert(key)
            withContext(Dispatchers.Main) {
                if (res.success) {
                    buzz(longArrayOf(0, 120))
                    _detail.value = "Help is on the way"
                    say("Your nurse has been notified.", Feedback.Kind.SUCCESS)
                    _uiState.value = UiState.Sent
                    resumeAfter(3_000, UiState.Sent)
                } else {
                    if (res.retryable) {
                        pending.addManual(key)
                        _pendingCount.value = pending.all().size
                        startRetryLoop()
                    }
                    failFeedback(res)
                }
            }
        }
    }

    // ── Speech upload ─────────────────────────────────────────────────────────

    private suspend fun handleSpeechDetected(audioSamples: FloatArray) {
        val rms = WavUtils.computeRms(audioSamples)
        if (rms < 0.004f) {
            Log.d(TAG, "Clip RMS=${"%.5f".format(rms)} — silence, not uploading")
            withContext(Dispatchers.Main) {
                _detail.value = "I didn't hear a message after \"Help\". Say it again, or tap Call Nurse."
                say("No message heard. Please try again or tap Call Nurse.", Feedback.Kind.ERROR)
                _uiState.value = UiState.Ignored
                resumeAfter(2_500, UiState.Ignored)
            }
            return
        }

        withContext(Dispatchers.Main) { _uiState.value = UiState.SpeechDetected }
        val key = PendingRequests.newKey()
        val wav = WavUtils.toWavByteArray(audioSamples)
        val result = serverUploader.uploadAudio(wav, key)

        withContext(Dispatchers.Main) {
            when {
                !result.success -> {
                    if (result.retryable) {
                        withContext(Dispatchers.IO) { pending.addAudio(key, wav) }
                        _pendingCount.value = pending.all().size
                        startRetryLoop()
                    }
                    failFeedback(result)
                }
                !result.shouldAlert -> {
                    Log.i(TAG, "Non-alert intent=${result.intent} — telling the patient")
                    val heard = result.transcript.trim()
                    _detail.value = if (heard.isNotEmpty())
                        "Heard \"${heard.take(60)}\" but no request was found. Tap Call Nurse if you need help."
                    else "No request was understood. Tap Call Nurse if you need help."
                    say("Your message didn't contain a request.", Feedback.Kind.INFO)
                    _uiState.value = UiState.Ignored
                    resumeAfter(4_000, UiState.Ignored)
                }
                else -> {
                    buzz(longArrayOf(0, 120))
                    val msg = result.message.ifBlank { "Nurse notified" }
                    _detail.value = msg
                    say(msg, Feedback.Kind.SUCCESS)
                    _uiState.value = UiState.Sent
                    resumeAfter(3_000, UiState.Sent)
                }
            }
        }
    }

    private fun failFeedback(res: UploadResult) {
        buzz(longArrayOf(0, 300, 150, 300))
        val why = res.message.ifBlank { "Could not reach the server" }
        _detail.value = if (res.retryable) "$why — retrying automatically" else why
        say(if (res.retryable) "Not sent yet: $why. Retrying — or tap Call Nurse."
            else "Not sent: $why.", Feedback.Kind.ERROR)
        _uiState.value = UiState.UploadError
        resumeAfter(4_000, UiState.UploadError)
    }

    // ── Retry queue ──────────────────────────────────────────────────────────

    private fun startRetryLoop() {
        if (retryJob?.isActive == true) return
        retryJob = viewModelScope.launch(Dispatchers.IO) {
            val backoff = longArrayOf(5_000, 10_000, 20_000, 40_000, 60_000)
            var round = 0
            while (isActive && !pending.isEmpty()) {
                delay(backoff[minOf(round, backoff.size - 1)])
                round++
                for (e in pending.all()) {
                    if (System.currentTimeMillis() - e.createdAt > PendingRequests.MAX_AGE_MS) {
                        pending.remove(e.key)
                        say("An earlier request could not be sent. If you still need help, tap Call Nurse.",
                            Feedback.Kind.ERROR)
                        continue
                    }
                    val res = when (e.type) {
                        "manual" -> serverUploader.sendManualAlert(e.key)
                        else -> {
                            val wav = pending.audio(e.key)
                            if (wav == null) { pending.remove(e.key); continue }
                            serverUploader.uploadAudio(wav, e.key)
                        }
                    }
                    when {
                        res.success && !res.shouldAlert -> {
                            pending.remove(e.key)
                            say("Your earlier recording was sent, but no request was heard in it. Tap Call Nurse if you need help.",
                                Feedback.Kind.INFO)
                            round = 0
                        }
                        res.success -> {
                            pending.remove(e.key)
                            say("Your earlier request was delivered: ${res.message.ifBlank { "nurse notified" }}.",
                                Feedback.Kind.SUCCESS)
                            round = 0
                        }
                        res.retryable -> pending.bump(e.key)
                        else -> {
                            pending.remove(e.key)
                            say("An earlier request was refused: ${res.message}", Feedback.Kind.ERROR)
                        }
                    }
                }
                _pendingCount.postValue(pending.all().size)
            }
            _pendingCount.postValue(pending.all().size)
        }
    }

    // ── Haptics (silent feedback) ────────────────────────────────────────────

    private fun buzz(pattern: LongArray) {
        try {
            val v = getApplication<Application>().getSystemService(Context.VIBRATOR_SERVICE) as? Vibrator ?: return
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                v.vibrate(VibrationEffect.createWaveform(pattern, -1))
            } else {
                @Suppress("DEPRECATION") v.vibrate(pattern, -1)
            }
        } catch (_: Exception) {}
    }

    // ── Lifecycle ─────────────────────────────────────────────────────────────

    override fun onCleared() {
        super.onCleared()
        fullStop()
        sileroVAD?.close()
        Log.i(TAG, "ViewModel cleared")
    }
}
