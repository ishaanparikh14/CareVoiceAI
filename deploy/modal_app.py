"""
modal_app.py — Deploy the CareVoice AI server to Modal on a GPU.

WHAT THIS DOES
--------------
- Builds a container image with the server code + all Python deps + CUDA libs.
- Requests a T4 GPU so Whisper `medium` transcribes in ~1-2s (2-3s end-to-end).
- Mounts the trained intent model from a Modal Volume (uploaded once — testers
  never download it).
- Serves the existing FastAPI app (server/main.py) at a public HTTPS URL,
  with WebSockets working for nurse alerts + patient audio streaming.

PREREQUISITES (run these once on your machine — see deploy/DEPLOY_MODAL.md)
--------------------------------------------------------------------------
  pip install modal
  modal token new
  # create the model volume and upload the trained model:
  modal volume create carevoice-models
  modal volume put carevoice-models ../server/storage/models/intent_ml /intent_ml
  # set secrets (Neon DB URL + app secrets):
  modal secret create carevoice-secrets \
      DATABASE_URL_OVERRIDE="postgresql://USER:PASS@HOST/carevoice?sslmode=require" \
      SECRET_KEY="<long-random-hex>" \
      NURSE_ADMIN_KEY="<your-nurse-key>"

DEPLOY
------
  modal deploy deploy/modal_app.py
  # → prints a public URL like  https://<you>--carevoice-fastapi.modal.run

The Android app / web dashboard then point at that HTTPS URL.
"""

from pathlib import Path

import modal

# ── Paths ─────────────────────────────────────────────────────────────────────
# This file lives in deploy/ ; the server package is ../server relative to it.
SERVER_DIR = Path(__file__).parent.parent / "server"

# ── Container image ─────────────────────────────────────────────────────────--
# Start from a CUDA-enabled base so faster-whisper (CTranslate2) finds cuBLAS/cuDNN.
image = (
    modal.Image.from_registry(
        "nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04",
        add_python="3.11",
    )
    .apt_install("ffmpeg", "libgomp1")
    .pip_install(
        "fastapi==0.111.0",
        "uvicorn[standard]==0.29.0",
        "python-multipart==0.0.9",
        "aiosqlite==0.20.0",
        "asyncpg==0.29.0",
        "python-jose[cryptography]==3.3.0",
        "passlib[argon2]==1.7.4",
        "pydantic==2.7.1",
        "pydantic-settings==2.2.1",
        "python-dotenv==1.0.1",
        "aiofiles==23.2.1",
        "numpy==1.26.4",
        "faster-whisper==1.0.3",
        "transformers==4.40.1",
        "torch==2.3.0",
    )
    # Copy the server source into the image at /root/server. Exclude local-only
    # artifacts so the cloud image never ships your dev DB, saved audio, caches,
    # or local .env (Modal secrets provide config instead).
    .add_local_dir(
        str(SERVER_DIR),
        remote_path="/root/server",
        ignore=[
            ".env",
            "storage/carevoice.db",
            "storage/wav_temp/**",
            "storage/models/**",          # model comes from the Volume, not the image
            "storage/voice_notes",        # voice-note audio lives on its own Volume
            "storage/voice_notes/**",
            "**/__pycache__/**",
            "ml/**",                       # training scripts not needed at runtime
        ],
    )
)

# ── Persistent volumes ──────────────────────────────────────────────────────--
# Intent model (uploaded once by you). Mounted read-only at the path the server
# already expects: server/storage/models/intent_ml.
model_volume = modal.Volume.from_name("carevoice-models", create_if_missing=True)

# Whisper weights cache — persist so `medium` downloads only on the first run,
# not on every cold start.
whisper_cache = modal.Volume.from_name("carevoice-whisper-cache", create_if_missing=True)

# Voice-note recordings (the sender's original audio). Persisted so a note can
# still be played back after the container restarts or is redeployed.
voice_notes_volume = modal.Volume.from_name("carevoice-voice-notes", create_if_missing=True)

app = modal.App("carevoice")


# ── GPU toggle ────────────────────────────────────────────────────────────────
# T4 GPUs give ~2-3s Whisper `medium` transcription, BUT when Modal is out of
# free GPU capacity every request queues ("waiting to be scheduled on a GPU_T4
# worker") and the dashboard won't even load. Set USE_GPU=False to run CPU-only:
# the dashboard, live alerts, voice, and department forwarding are all instant,
# and Whisper falls back to the `small` model on CPU (slower transcription, an
# accepted tradeoff when GPUs are unavailable). Flip back to True for full speed
# once capacity returns.
USE_GPU = True

_fn_kwargs = dict(
    image=image,
    volumes={
        "/root/server/storage/models/intent_ml": model_volume,
        "/root/.cache/huggingface": whisper_cache,
        "/root/server/storage/voice_notes": voice_notes_volume,
    },
    secrets=[modal.Secret.from_name("carevoice-secrets")],
    # Stay warm for 20 min after the last request to avoid cold starts.
    scaledown_window=1200,
    # High timeout so long-lived WebSocket connections (nurse alerts + patient
    # audio streaming) are not killed mid-session.
    timeout=3600,
    min_containers=1,               # keep ONE warm container always running
    max_containers=1,               # PIN to a single container so the in-memory
                                     # broadcaster + SQLite alerts DB are shared by
                                     # every patient and nurse (cross-container
                                     # broadcasts would otherwise be lost).
)
if USE_GPU:
    _fn_kwargs["gpu"] = "T4"


@app.function(**_fn_kwargs)
@modal.concurrent(max_inputs=50)    # one container handles all concurrent WS + requests
@modal.asgi_app()
def fastapi_app():
    """Return the existing FastAPI ASGI app, configured for cloud."""
    import os
    import sys

    # Real (non-stub) pipeline. Device + model size follow the GPU toggle:
    # GPU → Whisper 'medium' on CUDA (fast); CPU → 'small' on CPU (no GPU wait).
    os.environ.setdefault("USE_STUB", "false")
    if USE_GPU:
        os.environ.setdefault("WHISPER_MODEL", "medium")
        os.environ.setdefault("WHISPER_DEVICE", "cuda")
    else:
        os.environ.setdefault("WHISPER_MODEL", "small")
        os.environ.setdefault("WHISPER_DEVICE", "cpu")
    # Alerts DB (SQLite) → writable /tmp inside the container.
    os.environ.setdefault("DB_PATH", "/tmp/carevoice.db")

    # Make the server package importable, then import the app.
    sys.path.insert(0, "/root/server")
    from main import app as fastapi_application  # noqa: E402

    return fastapi_application
