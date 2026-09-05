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

## Intent model

The multilingual intent classifier is trained separately and stored in `server/storage/models/intent_ml/` (not tracked in git — it's ~500 MB). Training and evaluation scripts are in `server/ml/`:

```bash
cd server/ml
python train_intent_ml.py       # fine-tune mBERT on prepared_ml/*.csv
python evaluate_intent_ml.py     # report accuracy per language
```

Prepared training splits (`prepared_ml/train.csv`, `val.csv`, `test.csv`) are tracked so training is reproducible. Raw source datasets and generated audio are gitignored.

---

## Notes

- Patient audio is processed transiently in `storage/wav_temp/` and is not committed.
- The SQLite database (`storage/carevoice.db`) holds alerts/transcripts and is recreated empty on first run. User accounts live in PostgreSQL.
- This is a LAN-only system by design; do not expose it directly to the public internet.
