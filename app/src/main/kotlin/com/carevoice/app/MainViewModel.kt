package com.carevoice.app

import android.app.Application
import android.util.Log
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.LiveData
import androidx.lifecycle.MutableLiveData
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
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
        object Ignored           : UiState()
        object DownloadError     : UiState()
        object UploadError       : UiState()
    }

    // ── LiveData ──────────────────────────────────────────────────────────────

    private val _uiState  = MutableLiveData<UiState>(UiState.Idle)
    val uiState: LiveData<UiState> = _uiState

    private val _rmsLevel = MutableLiveData(0f)
    val rmsLevel: LiveData<Float> = _rmsLevel

    // ── Resources ─────────────────────────────────────────────────────────────

    private var sileroVAD:         SileroVAD?        = null
    private var audioRecorder:     AudioRecorder?    = null
    private var wakeWordDetector:  WakeWordDetector? = null
    private var manualRecordingJob: kotlinx.coroutines.Job? = null
    private val serverUploader = ServerUploader(application)

    // ── Model download ────────────────────────────────────────────────────────

    fun ensureModelReady() {
        val current = _uiState.value
        if (current is UiState.Downloading || current == UiState.ModelReady) return
        _uiState.value = UiState.Downloading(0)

        viewModelScope.launch {
            try {
                val modelPaths = ModelManager.ensureModelsReady(
                    context    = getApplication(),
                    onProgress = { pct ->
                        viewModelScope.launch(Dispatchers.Main) {
                            _uiState.value = UiState.Downloading(pct)
                        }
                    }
                )
                if (sileroVAD == null) {
                    sileroVAD = SileroVAD(modelPaths[ModelManager.SILERO_VAD.fileName]!!)
                }
                withContext(Dispatchers.Main) { _uiState.value = UiState.ModelReady }
            } catch (e: Exception) {
                Log.e(TAG, "Model download failed", e)
                withContext(Dispatchers.Main) { _uiState.value = UiState.DownloadError }
            }
        }
    }

    // ── Listening control ─────────────────────────────────────────────────────

    /**
     * Start wake-word detection.
     * When "help/nurse/emergency" is heard the app automatically records
     * the follow-up message and sends it to the nurse.
     * Falls back to energy-VAD if SpeechRecognizer is unavailable.
     */
fun startListening() {
    // Tear down any existing sessions cleanly
    audioRecorder?.stop()
    wakeWordDetector?.stop()
    manualRecordingJob?.cancel()

    val app: Application = getApplication()

    val detector = WakeWordDetector(app) {
        // "Help" detected — start recording the actual request
        viewModelScope.launch(Dispatchers.Main) {
            Log.i(TAG, "Wake word detected — starting recording")

            _uiState.value = UiState.Triggered

            // Pause wake-word detection while recording the request
            wakeWordDetector?.stop()

            startWakeWordRecording()
        }
    }

    if (detector.isAvailable()) {
        wakeWordDetector = detector
        detector.start()

        _uiState.value = UiState.WakeWordListening

        Log.i(TAG, "Wake-word listening started — waiting for Help")
    } else {
        // IMPORTANT:
        // Never fall back to Silero VAD here.
        // Silero VAD detects ANY speech and would create alerts
        // without the patient saying "Help".
        wakeWordDetector = null

        _uiState.value = UiState.Idle

        Log.e(
            TAG,
            "SpeechRecognizer unavailable — wake-word mode cannot start. " +
                "NOT starting AudioRecord/Silero VAD fallback."
        )
    }
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

    /**
     * Record audio immediately after the wake word fires.
     * Stops after 1.6 s of silence or 8 s max, then uploads.
     * After success/failure, resumes wake-word detection.
     */
    private fun startWakeWordRecording() {
        manualRecordingJob?.cancel()
        manualRecordingJob = viewModelScope.launch(Dispatchers.IO) {
            val sampleRate   = 16_000
            val chunkSamples = 512
            val maxSamples   = sampleRate * 8   // 8 s hard cap

            val minBuf = android.media.AudioRecord.getMinBufferSize(
                sampleRate,
                android.media.AudioFormat.CHANNEL_IN_MONO,
                android.media.AudioFormat.ENCODING_PCM_16BIT
            )
            val recorder = android.media.AudioRecord(
                android.media.MediaRecorder.AudioSource.VOICE_RECOGNITION,
                sampleRate,
                android.media.AudioFormat.CHANNEL_IN_MONO,
                android.media.AudioFormat.ENCODING_PCM_16BIT,
                minBuf * 4
            )

            if (recorder.state != android.media.AudioRecord.STATE_INITIALIZED) {
                Log.e(TAG, "AudioRecord failed to init in startWakeWordRecording")
                withContext(Dispatchers.Main) { startListening() }
                return@launch
            }

            recorder.startRecording()
            withContext(Dispatchers.Main) { _uiState.value = UiState.ManualRecording }

            val allSamples   = mutableListOf<FloatArray>()
            val shortBuf     = ShortArray(chunkSamples)
            var totalSamples = 0
            var silenceCount = 0

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

                    // Cut after 1.6 s silence, minimum 1 s recorded
                    silenceCount = if (rms < 0.018f) silenceCount + 1 else 0
                    if (totalSamples > sampleRate && silenceCount >= 50) break
                }
            } finally {
                try { recorder.stop(); recorder.release() } catch (_: Exception) {}
            }

            val utterance = allSamples.flatMap { it.toList() }.toFloatArray()
            handleSpeechDetected(utterance)
        }
    }

    // ── Stop / cancel ─────────────────────────────────────────────────────────

    /** Cancel active recording and resume wake-word detection. */
    fun stopListening() {
        if (manualRecordingJob?.isActive == true) {
            manualRecordingJob?.cancel()
            manualRecordingJob = null
            _rmsLevel.value = 0f
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
        _rmsLevel.value = 0f
        _uiState.value = UiState.Idle
    }

    // ── Instant manual call ───────────────────────────────────────────────────

    /**
     * Send a Critical/Emergency alert immediately — no audio, no recording.
     * Used by the "Call Nurse" button.
     */
    fun callNurseNow() {
        viewModelScope.launch(Dispatchers.IO) {
            withContext(Dispatchers.Main) { _uiState.value = UiState.SpeechDetected }
            val success = serverUploader.sendManualAlert()
            withContext(Dispatchers.Main) {
                if (success) {
                    _uiState.value = UiState.Sent
                    viewModelScope.launch {
                        delay(2_000)
                        if (_uiState.value == UiState.Sent) startListening()
                    }
                } else {
                    _uiState.value = UiState.UploadError
                    viewModelScope.launch {
                        delay(3_000)
                        if (_uiState.value == UiState.UploadError) startListening()
                    }
                }
            }
        }
    }

    // ── Speech upload ─────────────────────────────────────────────────────────

    private suspend fun handleSpeechDetected(audioSamples: FloatArray) {
        // Skip silent clips (mic muted / no real speech captured)
        val rms = WavUtils.computeRms(audioSamples)
        if (rms < 0.005f) {
            Log.d(TAG, "Clip RMS=${"%.5f".format(rms)} — silence, skipping upload")
            withContext(Dispatchers.Main) { startListening() }
            return
        }

        Log.i(TAG, "Uploading ${audioSamples.size} samples rms=${"%.4f".format(rms)}")
        withContext(Dispatchers.Main) { _uiState.value = UiState.SpeechDetected }

        val result = serverUploader.uploadAudio(WavUtils.toWavByteArray(audioSamples))

        withContext(Dispatchers.Main) {
            when {
                !result.success -> {
                    _uiState.value = UiState.UploadError
                    viewModelScope.launch {
                        delay(3_000)
                        if (_uiState.value == UiState.UploadError) startListening()
                    }
                }
                !result.shouldAlert -> {
                    Log.i(TAG, "Non-alert intent=${result.intent} — resuming wake word")
                    startListening()
                }
                else -> {
                    _uiState.value = UiState.Sent
                    viewModelScope.launch {
                        delay(2_000)
                        if (_uiState.value == UiState.Sent) startListening()
                    }
                }
            }
        }
    }

    // ── Lifecycle ─────────────────────────────────────────────────────────────

    override fun onCleared() {
        super.onCleared()
        fullStop()
        sileroVAD?.close()
        Log.i(TAG, "ViewModel cleared")
    }
}
