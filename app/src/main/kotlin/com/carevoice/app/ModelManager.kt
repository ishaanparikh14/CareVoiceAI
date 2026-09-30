package com.carevoice.app

/**
 * ModelManager — one-time initialisation gate the patient screen awaits before
 * it starts listening.
 *
 * Voice activity detection is energy-based (see [SileroVAD]) and needs no
 * downloaded model, so this simply signals readiness immediately. It exists as
 * a seam: if a downloaded model is ever reintroduced, the fetch belongs here.
 */
object ModelManager {

    /** Prepare on-device resources. Reports 100% immediately (nothing to fetch). */
    suspend fun ensureModelsReady(onProgress: (Int) -> Unit) {
        onProgress(100)
    }
}
