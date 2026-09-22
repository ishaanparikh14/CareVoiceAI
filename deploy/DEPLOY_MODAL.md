# Deploying CareVoice AI to the cloud (Modal + Neon)

This makes the server **accessible from anywhere over the internet** while keeping
**2–3 s latency** by running Whisper `medium` on a cloud **GPU**. Testers just
install the APK and point it at the public URL — no model download, no local server.

- **Modal** — serverless GPU host (pay-per-second, scales to zero, free credits).
- **Neon** — free managed PostgreSQL (users + patients).
- The 516 MB intent model is uploaded to Modal **once**.

> You run these commands on your machine. Each is explained. Where a value is
> `<LIKE_THIS>`, substitute your own.

---

## 0. Prerequisites

```bash
pip install modal
```

---

## 1. Create a free Neon Postgres database

1. Sign up at https://neon.tech (free tier, no card).
2. Create a project (call it `carevoice`).
3. Copy the **connection string**. It looks like:
   ```
   postgresql://USER:PASSWORD@ep-xxxx-xxxx.aws.neon.tech/carevoice?sslmode=require
   ```
   Keep this — it's your `DATABASE_URL_OVERRIDE`.

The server auto-creates tables and seeds sample nurse/patient accounts on first boot.

---

## 2. Set up Modal

```bash
modal token new        # opens a browser to link your Modal account
```

New Modal accounts include monthly free credits that comfortably cover testing.

---

## 3. Upload the trained intent model (once)

The model is NOT in the repo (~516 MB). Upload it to a Modal Volume:

```bash
# create the volume
modal volume create carevoice-models

# upload your local trained model into it
modal volume put carevoice-models server/storage/models/intent_ml /intent_ml
```

> Run this from the project root so the local path `server/storage/models/intent_ml`
> resolves. The Whisper `medium` weights are NOT uploaded — Modal downloads them
> automatically on first run and caches them in a second volume.

---

## 4. Store your secrets in Modal

Generate a strong JWT key first:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Then create the secret set (paste your Neon URL + the generated key):

```bash
modal secret create carevoice-secrets ^
  DATABASE_URL_OVERRIDE="postgresql://USER:PASSWORD@ep-xxxx.aws.neon.tech/carevoice?sslmode=require" ^
  SECRET_KEY="<paste-generated-hex>" ^
  NURSE_ADMIN_KEY="<your-nurse-signup-key>"
```

(On macOS/Linux use `\` for line continuation instead of `^`.)

---

## 5. Deploy

```bash
modal deploy deploy/modal_app.py
```

Modal builds the image (first time takes a few minutes) and prints a public URL:

```
https://<your-account>--carevoice-fastapi-app.modal.run
```

That's your server. Open `https://<...>.modal.run/` in a browser → login page.
Admin dashboard at `https://<...>.modal.run/admin`.

> **Cold start:** after the server is idle it scales to zero. The FIRST request
> then takes ~5–15 s (GPU spin-up + model load). While warm, requests are 2–3 s.
> A warm instance is kept for 5 minutes after each request.

---

## 6. Point the app / dashboards at it

- **Web dashboards:** just open the Modal URL in any browser, anywhere.
- **Android app:** on the login/settings screen, set the Server URL to the full
  `https://<...>.modal.run` (the app already upgrades `https→wss` for WebSockets).

Sample logins (seeded automatically): `nurse_anna` / `pass123`, `patient_raj` / `pass123`,
`admin` / `admin1234`. (Change these before any real use.)

---

## Updating after code changes

Re-run:

```bash
modal deploy deploy/modal_app.py
```

Only the changed layers rebuild. The model volume and DB persist.

---

## Costs & scaling notes

- **Scale to zero** (`min_containers=0`) → you pay only while requests are being
  processed. Idle = free.
- To eliminate cold starts during a demo, set `min_containers=1` in
  `deploy/modal_app.py` (keeps one GPU warm — uses credits continuously).
- T4 GPU is sufficient for `medium` + the intent model at your latency target.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Build fails on torch/cuda | Ensure the base image line in `modal_app.py` is the CUDA image; re-run deploy. |
| DB connection error | Check `DATABASE_URL_OVERRIDE` in the Modal secret; Neon needs `?sslmode=require`. |
| Model not found / keyword-only intents | Confirm `modal volume put` uploaded `intent_ml`; check `modal volume ls carevoice-models`. |
| First request slow, rest fast | Expected (cold start). Set `min_containers=1` to avoid it. |
| App can't connect | Use the full `https://` URL (not `http://`); phone has internet. |
