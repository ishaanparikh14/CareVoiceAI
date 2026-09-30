# CareVoice AI

**Voice-driven hospital nurse-call triage.** A patient speaks a request in **English, Hindi, or Kannada** — the system transcribes it, understands what they need, judges how urgent it is, and pushes a prioritized alert to the assigned nurse's dashboard in real time. When a conversation is needed, either side can start a peer-to-peer voice call.

The entire AI pipeline runs on infrastructure you control (a self-hosted GPU on [Modal](https://modal.com)). No patient audio is sent to third-party AI APIs.

---

## Table of contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [The AI pipeline](#the-ai-pipeline)
- [Tech stack](#tech-stack)
- [Repository layout](#repository-layout)
- [API reference](#api-reference)
- [Running locally](#running-locally)
- [Deploying to the cloud](#deploying-to-the-cloud)
- [Configuration](#configuration)
- [The intent model](#the-intent-model)
- [Security](#security)

---

## What it does

1. **Patient side (Android).** The app listens on-device for a wake word ("help", "nurse", "emergency"). On trigger it records the follow-up message, encodes it to WAV, and uploads it. There's also an instant **Call Nurse** button that raises a Critical alert with no audio.
2. **Server side.** The audio runs through a four-layer AI pipeline (speech-to-text → intent → NLP summary + emotion → priority), an alert is stored, and it is broadcast over a WebSocket to the nurse station.
3. **Nurse side (Android + web).** A live dashboard shows incoming alerts sorted by priority, filtered to the nurse's assigned rooms. Critical alerts are announced aloud via text-to-speech. Nurses acknowledge alerts and can place a voice call back to the patient.
4. **Real-time voice calls.** Patient and nurse can hold an audio call over **WebRTC** (peer-to-peer media; the server only relays signaling).
5. **Admin dashboard.** Hospital-wide monitoring: nurses, patients, rooms, alert analytics, nurse approvals, and room/patient management.

---

## Architecture

```
┌──────────────────┐                                  ┌──────────────────────┐
│  Android app     │                                  │  Nurse dashboard      │
│  (patient)       │                                  │  (Android + web)      │
│                  │   1. WAV upload (HTTPS POST)      │                       │
│  wake word +     │ ───────────────────────────────► │                       │
│  energy VAD      │        /audio/ingest              │                       │
└──────────────────┘                                  └──────────────────────┘
        │                          │                            ▲
        │                          ▼                            │
        │              ┌────────────────────────────┐          │ 2. live alert
        │              │  FastAPI server (Modal GPU) │          │    (WebSocket
        │              │                             │ ─────────┘    /ws/nurse)
        │              │  Whisper → DistilBERT →     │
        │              │  NLP summary → priority     │
        │              │  engine                     │
        │              └────────────────────────────┘
        │                          │
        │                          ├── SQLite   (alerts / transcripts)
        │                          └── Postgres (users / patients — Neon)
        │
        │   3. WebRTC voice call  (signaling relayed via /ws/signal;
        └──────────────────────────  Opus/SRTP media flows peer-to-peer)
```

**Why this shape**

- The server is **pinned to a single always-warm container** so the in-memory alert broadcaster and the SQLite alerts DB are shared by every connected patient and nurse. (Scaling out would require moving broadcasts to Redis pub/sub and alerts to Postgres.)
- **WebRTC media is peer-to-peer** — the backend never carries audio, only the setup handshake, keeping call latency low and server load flat.
- **Two databases by design:** transient, high-write alert data in SQLite; durable account/ward data in managed Postgres.

---

## The AI pipeline

Every uploaded utterance flows through four layers in `server/pipeline.py`, `nlp_summary.py`, and `priority_engine.py`:

### Layer 1 — Speech-to-text (Whisper)

- [`faster-whisper`](https://github.com/SYSTRAN/faster-whisper) running `medium` on GPU (`float16` on CUDA), falling back to `small` on CPU.
- **Robust multilingual handling.** Whisper misreads Indic speech in known ways, so the transcriber corrects them:
  - Urdu-script output is re-transcribed as Hindi (Devanagari).
  - Kannada speech that Whisper emits in Devanagari triggers a second forced-Kannada pass; whichever pass is more confident *and* script-consistent wins.

### Layer 2 — Intent classification (fine-tuned DistilBERT)

- A fine-tuned [`distilbert-base-multilingual-cased`](https://huggingface.co/distilbert/distilbert-base-multilingual-cased) classifies the transcript into one of **9 intents**: Emergency, Pain, Medication, Food/Water, Mobility, Hygiene, Emotional Support, Information, Other.
- The transformer produces contextual token embeddings; a classification head maps the pooled representation to the intent label — one forward pass, no external vector store.
- A **high-precision multilingual keyword override** runs alongside the model: when the transcript clearly contains an intent's keywords, that wins over the model prediction. This fixes model inconsistency on Whisper's Hindi/Kannada spelling variants.
- If the model can't load, the pipeline degrades gracefully to a pure keyword matcher.

### Layer 3 — NLP summary + emotional intelligence

`nlp_summary.py` turns the transcript into a nurse-readable message deterministically (no cloud, no cold start):

- **Text normaliser** — collapses diacritic/spelling variation (साँस / सांस / सास → one form) so keyword matching is robust.
- **Severity scorer** — maps to an emotional state (`calm` / `anxious` / `distressed` / `panicked`) with a numeric magnitude.
- **Request summariser** — produces a short description ("wants water", "chest pain — possible cardiac emergency").
- **Auto-message generator** — assembles the standardized alert: *"Patient X in Room Y is calling — the patient sounds distressed — chest pain — possible cardiac emergency."*

### Layer 4 — Priority engine (with a safety override)

`priority_engine.py` maps intent → **Critical / Urgent / Routine**:

- Emergency → Critical; Pain, Medication → Urgent; everything else → Routine.
- **Safety net:** a curated list of life-threatening phrases ("can't breathe", "chest pain", …) across English/Hindi/Kannada and multiple scripts forces **Critical** regardless of the model's guess. A "call the doctor" family of phrases forces at least **Urgent**.
- Unacknowledged **Urgent** alerts auto-escalate to **Critical** after a timeout so a forgotten request re-pages the nurse.

> The design principle throughout: **the model provides coverage, deterministic rules provide safety.** A misclassification should never silently downgrade a life-threatening request.

---

## Tech stack

| Layer | Technology |
|---|---|
| Android client | Kotlin, MVVM (ViewModel + LiveData), View Binding, Coroutines, OkHttp |
| Voice calls | WebRTC (`io.github.webrtc-sdk`), STUN/TURN, foreground service |
| On-device speech | Android `SpeechRecognizer` (wake word) + energy-based VAD |
| Backend | FastAPI (async Python), Uvicorn, WebSockets |
| Speech-to-text | faster-whisper (CTranslate2) |
| Intent model | HuggingFace Transformers + PyTorch (fine-tuned DistilBERT) |
| Databases | SQLite (alerts) · PostgreSQL / Neon (users + patients) |
| Auth | JWT (python-jose), Argon2 password hashing (passlib) |
| Deployment | Modal serverless GPU (NVIDIA T4 / CUDA 12.4) |

---

## Repository layout

```
.
├── app/                         # Android client (Kotlin)
│   ├── build.gradle
│   ├── proguard-rules.pro
│   └── src/main/
│       ├── AndroidManifest.xml
│       ├── kotlin/com/carevoice/app/
│       │   ├── LoginActivity / RegisterActivity      # auth screens
│       │   ├── MainActivity / MainViewModel          # patient home (wake word, upload)
│       │   ├── NurseActivity + AlertAdapter/Model     # nurse dashboard
│       │   ├── WakeWordDetector / SileroVAD           # on-device speech triggers
│       │   ├── AudioRecorder / WavUtils / ServerUploader
│       │   ├── NurseTts                               # spoken alert announcements
│       │   ├── CallActivity / IncomingCallActivity    # WebRTC call UI
│       │   ├── CallService / CallSession              # call lifecycle
│       │   ├── WebRtcCallManager / SignalingClient    # WebRTC + signaling
│       │   ├── ModelManager / UserSession
│       └── res/                                       # layouts, drawables, values
├── server/                      # FastAPI backend
│   ├── main.py                  # app entry, lifespan, router wiring, auto-escalation
│   ├── config.py                # settings (env-overridable)
│   ├── models.py                # Pydantic schemas / API contract
│   ├── pipeline.py              # real ASR + intent pipeline
│   ├── pipeline_stub.py         # stub pipeline (no ML, local dev)
│   ├── nlp_summary.py           # summary + emotion (Layer 3)
│   ├── priority_engine.py       # intent → priority + safety markers (Layer 4)
│   ├── intent_keywords.py       # multilingual keyword override
│   ├── database.py / pg_database.py   # SQLite + Postgres access
│   ├── routers/                 # auth, audio, alerts, admin, ws, patient_ws, signal
│   ├── static/                  # web dashboards (login / nurse / admin)
│   ├── ml/                      # training + eval scripts, prepared datasets
│   └── storage/                 # runtime: models, wav_temp, sqlite db (gitignored)
├── deploy/
│   ├── modal_app.py             # Modal deployment definition
│   └── DEPLOY_MODAL.md          # step-by-step deploy guide
├── build.gradle                 # Android root build config
└── README.md
```

---

## API reference

Interactive docs are served at `/docs` (Swagger). Core endpoints:

**Auth** (`/auth`)
| Method | Path | Purpose |
|---|---|---|
| POST | `/auth/login` | Log in (nurse or patient), returns JWT |
| GET | `/auth/me` | Current user profile |
| POST | `/auth/register/patient` | Patient signup |
| POST | `/auth/register/nurse` | Nurse signup (requires `NURSE_ADMIN_KEY`) |
| GET | `/auth/patients` | Patients assigned to the calling nurse |

**Audio + alerts**
| Method | Path | Purpose |
|---|---|---|
| POST | `/audio/ingest` | Upload WAV → run pipeline → create alert |
| POST | `/alerts/manual` | Instant Critical alert (no audio) |
| GET | `/alerts/latest` | Recent alerts (optionally unacked only) |
| GET | `/alerts/{id}` | Single alert |
| POST | `/alerts/{id}/ack` | Acknowledge an alert |

**WebSockets**
| Path | Purpose |
|---|---|
| `/ws/nurse` | Real-time alert push to nurse clients |
| `/ws/patient` | Always-on patient audio streaming (server-side VAD) |
| `/ws/signal` | WebRTC call signaling relay |

**Admin** (`/admin/*`, admin role) — hospital stats, nurse/patient/room management, alert analytics, nurse approvals, patient assignment/discharge.

**Health** — `GET /health` returns status, version, pipeline mode, and DB path.

---

## Running locally

### Prerequisites
- Python 3.11
- PostgreSQL (or a Neon connection string) for users + patients
- Android Studio / SDK for the client
- Optional: NVIDIA GPU + CUDA for fast Whisper inference

### 1. Backend

```powershell
cd server
python -m venv .venv
.\.venv\Scripts\activate            # Windows
# source .venv/bin/activate         # Linux/macOS
pip install -r requirements.txt

Copy-Item .env.example .env          # then edit — set the secrets below
```

Set at minimum in `server/.env`:

```
SECRET_KEY=<python -c "import secrets; print(secrets.token_hex(32))">
NURSE_ADMIN_KEY=<a key nurses must supply to register>
PG_PASSWORD=<your local postgres password>          # or use DATABASE_URL_OVERRIDE
USE_STUB=true                                        # skip ML for quick UI testing
```

Run it:

```powershell
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

- Login / nurse dashboard: `http://localhost:8000/`
- Admin dashboard: `http://localhost:8000/admin`

Tables and sample accounts are seeded automatically on first startup.

### 2. Android app

Open the project root in Android Studio, then build:

```powershell
.\gradlew.bat assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

The server URL is configurable in the app's Settings screen (defaults to the hosted deployment). Point it at `http://<your-machine-ip>:8000` for local testing.

---

## Deploying to the cloud

The backend deploys to Modal as a serverless GPU app. Full walkthrough in **[`deploy/DEPLOY_MODAL.md`](deploy/DEPLOY_MODAL.md)**. In short:

```powershell
pip install modal
modal token new                                      # link your Modal account

# one-time: create the model volume + upload the trained intent model
modal volume create carevoice-models
modal volume put carevoice-models server/storage/models/intent_ml /intent_ml

# one-time: store secrets (reused across deploys)
modal secret create carevoice-secrets `
  DATABASE_URL_OVERRIDE="postgresql://USER:PASS@HOST/db?sslmode=require" `
  SECRET_KEY="<64-hex>" `
  NURSE_ADMIN_KEY="<nurse-key>"

# deploy (rebuilds only changed layers)
modal deploy deploy/modal_app.py
```

The deploy prints a public HTTPS URL. Point the Android app (`ServerUploader.DEFAULT_SERVER_URL`) and web dashboards at it. GPU is toggled by `USE_GPU` in `deploy/modal_app.py` (`True` → T4/CUDA + Whisper `medium`; `False` → CPU + `small`).

---

## Configuration

All settings live in `server/config.py`, overridable via environment variables, `server/.env`, or a Modal secret.

| Variable | Purpose | Default |
|---|---|---|
| `USE_STUB` | Use the stub pipeline (no ML) | `false` |
| `WHISPER_MODEL` | Whisper size (`small` / `medium` / …) | `medium` |
| `WHISPER_DEVICE` | `cuda` or `cpu` | `cuda` |
| `DATABASE_URL_OVERRIDE` | Full Postgres URL (e.g. Neon) — wins over `PG_*` | — |
| `PG_PASSWORD` | Postgres password (local dev) | — (must set) |
| `SECRET_KEY` | JWT signing key | — (**must set**; auth fails if empty) |
| `NURSE_ADMIN_KEY` | Key required to register a nurse | — (**must set**; registration disabled if empty) |
| `CORS_ORIGINS` | Allowed origins | empty → all (with warning) |
| `ESCALATE_URGENT_AFTER_SECONDS` | Urgent→Critical auto-escalation delay | `90` |

The server logs a security warning on startup if `SECRET_KEY` or `NURSE_ADMIN_KEY` is unset.

---

## The intent model

The trained classifier (`intent_ml`, ~516 MB) is **not committed to git** (exceeds GitHub file-size limits). Two ways to run:

### Option A — Stub mode (no model, fastest)
Set `USE_STUB=true`. Login, dashboards, alerts, WebSocket, and calls all work; only intent classification is simplified. No GPU or download needed.

### Option B — Train the real model (reproducible)
The prepared splits are committed under `server/ml/data/prepared_ml/`. Fine-tune `distilbert-base-multilingual-cased` on the trilingual 9-intent dataset:

```powershell
cd server/ml
python train_intent_ml.py           # writes to server/storage/models/intent_ml/
python evaluate_intent_ml.py         # optional: per-language accuracy
```

Training uses **weighted cross-entropy** (weights inversely proportional to class frequency) to handle class imbalance, tracks **macro-F1**, 8 epochs, LR 3e-5, max sequence length 64. A GPU trains it in a few minutes; CPU works but is slower.

The server loads from exactly `server/storage/models/intent_ml/` (`model.safetensors`, `config.json`, `tokenizer.json`, `vocab.txt`, …). If you obtain the folder another way, drop it there and set `USE_STUB=false`.

Whisper weights download automatically on first run via `faster-whisper` — nothing to do manually.

---

## Security

- **Secrets are never committed.** `server/.env` is gitignored; `SECRET_KEY`, `NURSE_ADMIN_KEY`, and the database URL are provided via env vars or a Modal secret. `config.py` ships with **empty** defaults and warns loudly at startup if they're missing.
- **Auth:** JWT bearer tokens (8-hour expiry, one shift); passwords hashed with Argon2. Nurse registration is gated by `NURSE_ADMIN_KEY`.
- **Media privacy:** patient audio is processed transiently in `storage/wav_temp/` and is not retained long-term; WebRTC call audio flows peer-to-peer and never touches the server.
- **CORS** should be restricted to your dashboard origins in production (`CORS_ORIGINS`).
