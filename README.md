# CareVoice AI

Voice-driven hospital nurse-call triage. A patient speaks a request in **English, Hindi, or Kannada**; the system transcribes it, classifies the intent, assigns a priority (**Critical / Urgent / Routine**), and pushes a real-time alert to the assigned nurse's dashboard.

Runs entirely on-premises over the hospital LAN — no cloud, no patient audio leaving the ward.

---

## Architecture

```
┌─────────────┐   audio    ┌──────────────────────────────────────┐   WebSocket   ┌──────────────┐
│ Android app │ ─────────► │ FastAPI server                        │ ────────────► │ Nurse        │
│ (patient)   │  /audio    │  ASR (Whisper) → Intent (mBERT)        │   real-time   │ dashboard    │
│ VAD + wake  │  /ingest   │  → keyword override → priority engine  │    alerts     │ (web)        │
└─────────────┘            └──────────────────────────────────────┘               └──────────────┘
```

**Pipeline stages**
1. **VAD + wake word** (on-device, Android) — mic only sends speech after the trigger word.
2. **ASR** — Whisper `small` on GPU (float16). Urdu output is forced back to Hindi/Devanagari.
3. **Intent classification** — fine-tuned multilingual BERT (`intent_ml`).
4. **Keyword override + safety markers** — corrects known ASR spelling drift and guarantees Critical/Urgent phrases are never downgraded.
5. **Priority engine** — intent-driven: Emergency → Critical, Pain/Medication → Urgent, else Routine.

---

## Repository layout

```
.
├── app/                    # Android client (Kotlin)
│   └── src/main/           # activities, VAD, wake-word, audio upload
├── server/                 # FastAPI backend
│   ├── main.py             # app entry / lifespan
│   ├── config.py           # settings (env-overridable)
│   ├── pipeline.py         # real ASR + intent pipeline
│   ├── pipeline_stub.py    # stubbed pipeline (no ML)
│   ├── priority_engine.py  # intent → priority mapping
│   ├── intent_keywords.py  # keyword override + safety markers
│   ├── routers/            # auth, audio, alerts, admin, websocket
│   ├── static/             # web dashboards (nurse / patient / admin / login)
│   ├── ml/                 # training + evaluation scripts and prepared data
│   └── storage/            # runtime: models, wav_temp, sqlite db (gitignored)
├── build.gradle            # Android build config
└── README.md
```

---

## Getting started

### Prerequisites
- Python 3.10+
- PostgreSQL (users + patients)
- Android Studio / SDK (for the client)
- Optional: NVIDIA GPU + CUDA for fast Whisper inference

### 1. Server setup

```bash
cd server
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/macOS
pip install -r requirements.txt

cp .env.example .env            # then edit PG_PASSWORD etc.
```

Create the PostgreSQL database referenced by `PG_DATABASE` (default `carevoice`). Tables and sample nurse/patient accounts are seeded automatically on first startup.

### 2. Run the server

```bash
# from the server/ directory
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

- Nurse / patient dashboard: `http://<server-lan-ip>:8000/`
- Admin dashboard: `http://<server-lan-ip>:8000/admin`

### 3. Android app

Open the project root in Android Studio, set the server LAN IP in the app config, then:

```bash
./gradlew assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

---

## Configuration

All settings live in `server/config.py` and can be overridden via environment variables or `server/.env`. Key ones:

| Variable | Purpose | Default |
|---|---|---|
| `USE_STUB` | Use stub pipeline (no ML) | `true` |
| `WHISPER_MODEL` | Whisper model size | `small` |
| `WHISPER_DEVICE` | `cuda` or `cpu` | `cuda` |
| `PG_PASSWORD` | PostgreSQL password | — (set in `.env`) |
| `SECRET_KEY` | JWT signing key | dev placeholder (**change in prod**) |
| `NURSE_ADMIN_KEY` | Nurse signup key | dev placeholder (**change in prod**) |

---

## Getting the intent model

The multilingual intent classifier (`intent_ml`) is **not committed to git** — the trained
weights are ~516 MB, which exceeds GitHub's file-size limits. You have two ways to run the
project depending on whether you need real ML classification.

### Option A — Run without the model (stub mode, fastest)

For UI/dashboard testing you don't need the model at all. Set stub mode and the server uses
a rule-based placeholder classifier:

```bash
# in server/.env
USE_STUB=true
```

Everything (login, dashboards, alerts, WebSocket) works; only the intent classification is
simplified. No GPU, no download, no training required.

### Option B — Train the real model (fully reproducible)

The prepared training splits are committed (`server/ml/data/prepared_ml/{train,val,test}.csv`),
so you can regenerate the exact model yourself. It fine-tunes
`distilbert-base-multilingual-cased` on the trilingual (en/hi/kn) 9-intent dataset.

**1. Install the ML dependencies** (already in `requirements.txt`):

```bash
pip install -r server/requirements.txt
# transformers, torch, faster-whisper are the heavy ones
```

**2. Train** (writes to `server/storage/models/intent_ml/` automatically):

```bash
cd server/ml
python train_intent_ml.py
```

- With an NVIDIA GPU (CUDA) this takes only a few minutes.
- On CPU it still runs, just slower.
- Output files land in `server/storage/models/intent_ml/`
  (`model.safetensors`, `config.json`, `tokenizer.json`, `vocab.txt`, …).

**3. Verify accuracy** (optional):

```bash
python evaluate_intent_ml.py     # reports overall + per-language accuracy
```

**4. Enable the real pipeline:**

```bash
# in server/.env
USE_STUB=false
```

### Where the model must live

The server loads the model from this exact path (see `pipeline.py`):

```
server/storage/models/intent_ml/
├── config.json
├── model.safetensors
├── tokenizer.json
├── tokenizer_config.json
├── special_tokens_map.json
└── vocab.txt
```

If you obtain the trained folder another way (shared drive, release asset, etc.), just drop
it in at that path and set `USE_STUB=false`.

### Whisper ASR & Silero VAD models

- **Whisper** weights download automatically on first run via `faster-whisper` — nothing to
  do manually. Control the size/device with `WHISPER_MODEL` / `WHISPER_DEVICE`.
- **Silero VAD** (`server/storage/models/silero_vad.onnx`) is optional on the server; if it's
  absent the server falls back to an energy-threshold VAD. The Android app downloads its own
  copy. To use it server-side, place the `.onnx` file at that path.

---

## Notes

- Patient audio is processed transiently in `storage/wav_temp/` and is not committed.
- The SQLite database (`storage/carevoice.db`) holds alerts/transcripts and is recreated empty on first run. User accounts live in PostgreSQL.
- This is a LAN-only system by design; do not expose it directly to the public internet.
