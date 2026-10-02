# CareVoice AI — Setup Guide (PC + Phone)

This is a step-by-step guide to get the whole system running: the **FastAPI server on a PC**
and the **Android app on a phone**, talking to each other over the same Wi-Fi.

> **Golden rule:** the PC and the phone must be on the **same Wi-Fi network**. This is a
> LAN-only system — the phone reaches the PC by its local IP address.

---

## Part 0 — What you'll end up with

- A server running on your PC at `http://<your-pc-ip>:8000`
- Web dashboards (nurse / patient / admin) openable in any browser on the network
- The Android app on your phone, pointed at your PC's IP, sending voice requests

---

## Part 1 — Server setup (PC)

### 1.1 Install the prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | 3.10 or newer | https://www.python.org/downloads/ (tick "Add to PATH") |
| Git | latest | https://git-scm.com/downloads |
| PostgreSQL | 14+ | https://www.postgresql.org/download/ — remember the password you set for the `postgres` user |
| (Optional) NVIDIA GPU + CUDA 12 | — | Only for fast Whisper speech-to-text. Works on CPU too, just slower. |

### 1.2 Clone the repo

```bash
git clone https://github.com/ishaanparikh14/CareVoiceAI.git
cd CareVoiceAI
```

### 1.3 Create a Python virtual environment and install dependencies

**Windows (PowerShell):**
```powershell
cd server
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**macOS / Linux:**
```bash
cd server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> This installs FastAPI, PyTorch, transformers, and faster-whisper. It's a few hundred MB and
> may take several minutes the first time.

### 1.4 Create the PostgreSQL database

Open a terminal and run (enter the `postgres` password when asked):

```bash
psql -U postgres -c "CREATE DATABASE carevoice;"
```

The server auto-creates all tables and seeds sample accounts on first startup — no manual SQL.

### 1.5 Configure `.env`

Copy the example and edit it:

```bash
# from the server/ directory
cp .env.example .env      # Windows PowerShell: Copy-Item .env.example .env
```

Open `server/.env` and set at least your PostgreSQL password:

```ini
PG_PASSWORD=your-postgres-password

# Quick start with NO machine-learning model needed:
USE_STUB=true
```

- `USE_STUB=true` → runs instantly, no model or GPU required. Great for a first test.
- `USE_STUB=false` → real speech + intent pipeline. Requires the trained model (see Part 4).

### 1.6 Find your PC's LAN IP address

You'll need this for the phone.

**Windows:**
```powershell
ipconfig
```
Look for **IPv4 Address** under your Wi-Fi adapter, e.g. `192.168.1.9`.

**macOS / Linux:**
```bash
ipconfig getifaddr en0        # macOS
hostname -I                   # Linux
```

Write it down — call it `<PC-IP>`.

### 1.7 Run the server

```bash
# from the server/ directory, with the venv active
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

`--host 0.0.0.0` is important — it makes the server reachable from the phone, not just the PC.

Leave this terminal running. You should see startup logs ending with the server listening on
port 8000.

### 1.8 Open the firewall (Windows only)

Windows Firewall blocks incoming connections by default, so the phone can't reach the server
until you allow port 8000. The repo includes a helper:

```powershell
# run PowerShell as Administrator, from the project root
.\open_firewall.bat
```

Or add the rule manually:
```powershell
New-NetFirewallRule -DisplayName "CareVoice 8000" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow
```

### 1.9 Verify it works

- On the PC browser: open `http://localhost:8000/` → you should see the login page.
- On the **phone's** browser (same Wi-Fi): open `http://<PC-IP>:8000/` → same login page.

If the phone can load that page, the network path works and the app will work too.

**Sample login accounts** (seeded automatically):

| Role | Username | Password |
|---|---|---|
| Nurse | `nurse_anna` | `pass123` |
| Patient | `patient_raj` | `pass123` |
| Admin | `admin` | `admin1234` |

> If these passwords don't work, the DB may have been seeded earlier with different ones —
> just register a fresh account from the app or the web login page.

---

## Part 2 — Android app setup (Phone)

You have two ways to get the app onto the phone. **Option A (build it) is recommended.**

### Option A — Build from source with Android Studio

**Prerequisites on the PC:**
- Android Studio (latest) — https://developer.android.com/studio
- It bundles the Android SDK (compileSdk 34) and Gradle. The app needs **minSdk 26**
  (Android 8.0) or newer on the phone.

**Steps:**

1. In Android Studio: **File → Open** → select the project root (`CareVoiceAI` folder).
2. Let Gradle sync finish (downloads dependencies the first time).
3. On the phone: enable **Developer Options** and **USB debugging**
   (Settings → About phone → tap "Build number" 7 times, then Settings → Developer options →
   USB debugging).
4. Connect the phone via USB, accept the "Allow USB debugging?" prompt.
5. Pick your phone in the device dropdown, then click **Run** (▶).

The app installs and launches automatically.

**Build an APK instead (to share the file):**
```bash
# from the project root
./gradlew assembleDebug           # Windows: .\gradlew.bat assembleDebug
```
The APK lands at `app/build/outputs/apk/debug/app-debug.apk`. Copy it to the phone and open it
to install (allow "install from unknown sources" if prompted).

### Option B — Install a prebuilt APK

If someone hands you an `app-debug.apk`, copy it to the phone and tap to install. Enable
"Install unknown apps" for your file manager if Android asks.

---

## Part 3 — Connect the app to the server

1. Open the **CareVoice** app. You'll see a login screen with a **Server URL** field.
2. Set the Server URL to your PC's address:
   ```
   http://<PC-IP>:8000
   ```
   e.g. `http://192.168.1.9:8000`  (include `http://` and the `:8000` port).
3. Log in with a seeded account, or tap **Register** to create a new patient/nurse.
   - Registering a **nurse** requires the nurse admin key — default `carevoice-nurse-2024`
     (set by `NURSE_ADMIN_KEY`).
4. Grant the **microphone** permission when asked (needed for voice requests).

That's it. As a patient, use the mic / Call Nurse button; as a nurse, watch requests arrive
live on the dashboard.

> You can change the Server URL later anytime from the app's **Settings** dialog (no reinstall).

---

## Part 4 — (Optional) Enable the real ML pipeline

Stub mode (`USE_STUB=true`) is fine for demoing the flow. For real speech-to-text + intent
classification, you need the trained intent model. It is **not** in the repo (~516 MB).

### Train it yourself (fully reproducible — data is included)

```bash
# from the server/ directory, venv active
cd ml
python train_intent_ml.py        # writes to ../storage/models/intent_ml/
python evaluate_intent_ml.py      # optional: prints accuracy
```

- A few minutes on GPU, longer on CPU.
- Output must end up at `server/storage/models/intent_ml/` (the training script does this
  automatically).

Then flip the flag and restart the server:

```ini
# server/.env
USE_STUB=false
```

**Whisper** (speech-to-text) weights download automatically on first run — nothing to do.
Optional overrides in `.env`:
```ini
# WHISPER_MODEL=small
# WHISPER_DEVICE=cuda      # use "cpu" if you have no NVIDIA GPU
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Phone browser can't open `http://<PC-IP>:8000` | Same Wi-Fi? Firewall rule added (step 1.8)? Server started with `--host 0.0.0.0`? |
| App says "upload failed / could not reach server" | Wrong Server URL in app, or PC IP changed. Re-check `ipconfig` and update the app's Server URL. |
| PC IP keeps changing | Set a static/reserved IP for the PC in your router, or re-enter the new IP in the app each time. |
| "connection failed: timeout" | Firewall or antivirus blocking port 8000. Allow it, or temporarily disable to test. |
| Server won't start — DB error | Is PostgreSQL running? Does the `carevoice` database exist? Is `PG_PASSWORD` correct in `.env`? |
| Login passwords rejected | DB seeded earlier with different passwords — register a new account instead. |
| Whisper error about `cublas` / CUDA | Set `WHISPER_DEVICE=cpu` in `.env`, or install the NVIDIA CUDA 12 runtime. |
| App won't install | Phone must be Android 8.0 (minSdk 26) or newer; enable install from unknown sources. |

---

## Quick reference

```bash
# Start the server (from server/, venv active)
python -m uvicorn main:app --host 0.0.0.0 --port 8000

# Dashboards
http://<PC-IP>:8000/        # nurse / patient (by login role)
http://<PC-IP>:8000/admin   # admin

# Build the Android app
./gradlew assembleDebug     # APK at app/build/outputs/apk/debug/
```
