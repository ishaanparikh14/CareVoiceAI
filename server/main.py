"""
main.py — FastAPI application entry point for the CareVoice AI server.

Startup sequence
----------------
1.  Lifespan context manager runs database initialisation (DDL / IF NOT EXISTS).
2.  Routers are registered: audio, alerts, WebSocket.
3.  The WebSocket ConnectionManager's broadcast coroutine is injected into the
    audio router so it can push alerts without a circular import.
4.  CORS middleware is applied (LAN-only origins by default).
5.  A /health endpoint confirms the server is up and reports stub mode.

Run with:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload
or use the provided run.ps1 / run.sh scripts.
"""

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import asyncio

import aiosqlite

from config import settings
from database import init_db, get_stale_unacked_urgent, escalate_alert, get_alert_by_id
from pg_database import create_pool, close_pool, init_pg_db
from models import HealthResponse, AlertResponse, WsAlertPayload
from routers import audio as audio_router
from routers import alerts as alerts_router
from routers import ws as ws_router
from routers import auth_router
from routers import patient_ws as patient_ws_router
from routers import admin as admin_router
from routers import signal as signal_router
from routers import voice_notes as voice_notes_router

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level   = settings.LOG_LEVEL.upper(),
    format  = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt = "%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── Auto-escalation background loop ───────────────────────────────────────────

async def _escalation_loop():
    """
    Periodically scan for unacknowledged Urgent alerts that have been waiting
    longer than ESCALATE_URGENT_AFTER_SECONDS and bump them to Critical, then
    re-broadcast so the nurse dashboard upgrades the card and re-announces it.

    Runs as a background task for the lifetime of the server. Opens its own
    short-lived SQLite connection (it can't use the request-scoped get_db dep).
    """
    after   = settings.ESCALATE_URGENT_AFTER_SECONDS
    every   = max(5, settings.ESCALATE_CHECK_INTERVAL_SECONDS)
    if after <= 0:
        logger.info("Auto-escalation disabled (ESCALATE_URGENT_AFTER_SECONDS=0)")
        return

    logger.info("Auto-escalation active: Urgent → Critical after %ds (check every %ds)", after, every)
    while True:
        try:
            await asyncio.sleep(every)
            async with aiosqlite.connect(settings.DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                stale = await get_stale_unacked_urgent(db, older_than_seconds=after)
                for row in stale:
                    changed = await escalate_alert(db, row["id"])
                    if not changed:
                        continue
                    updated = await get_alert_by_id(db, row["id"])
                    if not updated:
                        continue
                    payload = WsAlertPayload.from_alert_response(
                        AlertResponse.from_db_row(updated), event="alert_updated"
                    )
                    await ws_router.manager.broadcast(payload.model_dump_json())
                    logger.info("Broadcast escalation for alert %d (room %s)", row["id"], row["room_id"])
        except asyncio.CancelledError:
            logger.info("Escalation loop cancelled — shutting down")
            break
        except Exception as exc:
            logger.warning("Escalation loop error (continuing): %s", exc)


# ── Auto-reroute background loop ──────────────────────────────────────────────

async def _reroute_loop():
    """
    Periodically find alerts that were routed to a specific nurse but remain
    unacknowledged past REROUTE_UNACKED_AFTER_SECONDS, and reroute each to the
    next available nurse assigned to that patient (skipping the one who didn't
    ack). Falls back to broadcast when nobody assigned is free.

    This is the scheduling failover's time dimension: the initial route skips
    nurses who are busy *now*; this loop handles a nurse who received an alert
    but never responded.
    """
    after = settings.REROUTE_UNACKED_AFTER_SECONDS
    every = max(5, settings.REROUTE_CHECK_INTERVAL_SECONDS)
    if after <= 0:
        logger.info("Auto-reroute disabled (REROUTE_UNACKED_AFTER_SECONDS=0)")
        return

    logger.info("Auto-reroute active: unacked routed alerts re-sent after %ds (check every %ds)", after, every)
    from routing import reroute_stale
    while True:
        try:
            await asyncio.sleep(every)
            async with aiosqlite.connect(settings.DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                n = await reroute_stale(db, older_than_seconds=after)
                if n:
                    logger.info("Auto-reroute: re-sent %d stale alert(s)", n)
        except asyncio.CancelledError:
            logger.info("Reroute loop cancelled — shutting down")
            break
        except Exception as exc:
            logger.warning("Reroute loop error (continuing): %s", exc)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Runs once at startup (before the first request) and once at shutdown.

    Startup:
      • Initialise SQLite schema (CREATE TABLE IF NOT EXISTS — safe to replay).
      • Wire the WebSocket broadcaster into the audio router.
      • Log the active configuration.

    Shutdown:
      • Nothing to explicitly close — aiosqlite connections are per-request
        and aiofiles handles are closed after each write.
    """
    # ── Startup ───────────────────────────────────────────────────────────────
    logger.info("CareVoice AI server starting up")
    logger.info("  Host        : %s:%d", settings.HOST, settings.PORT)
    logger.info("  DB path     : %s",    settings.DB_PATH)
    logger.info("  PostgreSQL  : %s:%d/%s", settings.PG_HOST, settings.PG_PORT, settings.PG_DATABASE)
    logger.info("  WAV temp dir: %s",    settings.WAV_TEMP_DIR)
    logger.info("  Pipeline    : %s",    "STUB" if settings.USE_STUB else "REAL")

    # Guard against unset security keys. config.py leaves these empty by
    # default; they must be provided via SECRET_KEY / NURSE_ADMIN_KEY env vars
    # (or a Modal secret) before deploy.
    if not settings.SECRET_KEY:
        logger.critical(
            "SECRET_KEY is not set — JWT authentication will fail. "
            "Set SECRET_KEY in your .env, environment, or Modal secret."
        )
    if not settings.NURSE_ADMIN_KEY:
        logger.warning(
            "NURSE_ADMIN_KEY is not set — nurse registration is disabled. "
            "Set NURSE_ADMIN_KEY in your .env, environment, or Modal secret."
        )

    # SQLite — alerts/transcripts
    await init_db()

    # PostgreSQL — users + patients (opens pool, runs DDL, seeds if empty)
    await create_pool()
    await init_pg_db()

    # Inject nurse broadcaster into both the HTTP audio router AND the patient WS router
    audio_router.router
    from routers.audio import set_broadcaster
    from routers.patient_ws import set_nurse_broadcaster
    set_broadcaster(ws_router.manager.broadcast)
    set_nurse_broadcaster(ws_router.manager.broadcast)
    logger.info("WebSocket broadcaster wired to audio router and patient WS router")

    # Start the auto-escalation background task (Urgent → Critical after timeout).
    escalation_task = asyncio.create_task(_escalation_loop())
    # Start the auto-reroute background task (unacked routed alert → next nurse).
    reroute_task = asyncio.create_task(_reroute_loop())

    # Pre-warm the Whisper model so the first real request doesn't timeout
    # waiting for a 150 MB download + GPU load.  Runs in a thread executor so
    # it doesn't block the event loop during startup.
    if not settings.USE_STUB:
        loop = asyncio.get_event_loop()
        try:
            logger.info("Pre-warming Whisper + DistilBERT models (may take 30-60s on first run)...")
            import pipeline as real_pipeline
            await loop.run_in_executor(None, real_pipeline._get_whisper)
            await loop.run_in_executor(None, real_pipeline._get_intent_model)
            logger.info("Whisper + intent models ready")
        except Exception as exc:
            logger.warning("Whisper pre-warm failed (will retry on first request): %s", exc)

    yield  # ← server is live

    # ── Shutdown ──────────────────────────────────────────────────────────────
    escalation_task.cancel()
    reroute_task.cancel()
    for _t in (escalation_task, reroute_task):
        try:
            await _t
        except (asyncio.CancelledError, Exception):
            pass
    await close_pool()
    logger.info("CareVoice AI server shutting down")


# ── Application factory ───────────────────────────────────────────────────────

app = FastAPI(
    title       = "CareVoice AI — FastAPI Gateway",
    description = (
        "On-premises REST + WebSocket server for the CareVoice AI nurse-call "
        "triage system.  Receives WAV audio from patient phones, runs the AI "
        "pipeline, stores prioritised alerts, and pushes real-time notifications "
        "to nurse devices.  All traffic stays on the hospital LAN."
    ),
    version     = "0.1.0",
    lifespan    = lifespan,
    # Disable the default /redoc route to reduce surface area; /docs (Swagger)
    # is retained for development convenience.
    redoc_url   = None,
)


# ── CORS ──────────────────────────────────────────────────────────────────────
# Handle CORS origins configuration
# - Empty list ([]) means allow all origins (development default)
# - Specific list restricts to those origins (production)
# - ["*"] explicitly allows all origins
cors_origins = settings.CORS_ORIGINS
if not cors_origins:  # Empty list
    logger.warning("CORS_ORIGINS is empty. Allowing all origins for development.")
    logger.warning("Set CORS_ORIGINS in .env for production (e.g., specific IPs or domains).")
    cors_origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins     = cors_origins,
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)


# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(audio_router.router)        # POST /audio/ingest
app.include_router(alerts_router.router)       # GET  /alerts/latest, /alerts/{id}, POST /alerts/{id}/ack
app.include_router(ws_router.router)           # WS   /ws/nurse
app.include_router(auth_router.router)         # POST /auth/login, GET /auth/me, GET /auth/patients
app.include_router(patient_ws_router.router)   # WS   /ws/patient  (always-on VAD streaming)
app.include_router(signal_router.router)       # WS   /ws/signal   (WebRTC call signaling relay)
app.include_router(voice_notes_router.router)  # POST /voice-notes, GET /voice-notes/latest, /{id}/audio

# ── Static pages (registered BEFORE admin API router so /admin exact match ──
# serves the HTML page while /admin/stats etc. route into the API)
_static_dir = settings.BASE_DIR / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

_NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

@app.get("/dashboard", include_in_schema=False)
async def dashboard():
    """Serve the main app dashboard (requires auth — JS handles redirect)."""
    return FileResponse(str(_static_dir / "index.html"), headers=_NO_CACHE)

@app.get("/login", include_in_schema=False)
async def login_page():
    """Serve the login page."""
    return FileResponse(str(_static_dir / "login.html"))

@app.get("/admin", include_in_schema=False)
async def admin_page():
    """Serve the admin monitoring dashboard (requires admin role — JS handles guard)."""
    return FileResponse(str(_static_dir / "admin.html"))

@app.get("/", include_in_schema=False)
async def root():
    """Root redirects to login."""
    return FileResponse(str(_static_dir / "login.html"))


@app.get("/app/version", tags=["App Update"], summary="Latest Android app version manifest")
async def app_version():
    """Return the latest published Android build so installed apps can self-update.

    Reads static/app_version.json if present; otherwise returns a safe default
    that matches the shipped build (so the app never thinks an update exists
    when one hasn't been published).
    """
    import json
    manifest = _static_dir / "app_version.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            return JSONResponse(data, headers=_NO_CACHE)
        except Exception as exc:
            logger.warning("Bad app_version.json: %s", exc)
    # Default: no update beyond the baseline build.
    return JSONResponse(
        {
            "versionCode": 1,
            "versionName": "1.0",
            "apkUrl": "/static/carevoice-latest.apk",
            "changelog": "",
            "mandatory": False,
        },
        headers=_NO_CACHE,
    )

app.include_router(admin_router.router)        # GET  /admin/*     (admin monitoring — requires admin role)


# ── Health check ──────────────────────────────────────────────────────────────

@app.get(
    "/health",
    response_model = HealthResponse,
    tags           = ["Meta"],
    summary        = "Server health check",
    description    = "Returns HTTP 200 when the server is running. Includes stub mode flag.",
)
async def health():
    return HealthResponse(
        status   = "ok",
        version  = "0.1.0",
        use_stub = settings.USE_STUB,
        db_path  = str(settings.DB_PATH),
    )


# ── 404 handler ───────────────────────────────────────────────────────────────

@app.exception_handler(404)
async def not_found_handler(request, exc):
    return JSONResponse(
        status_code = 404,
        content     = {"detail": f"Route not found: {request.method} {request.url.path}"},
    )


# ── Dev entrypoint ────────────────────────────────────────────────────────────
# Allows  python main.py  during development. Production uses run.ps1 / run.sh.

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host      = settings.HOST,
        port      = settings.PORT,
        log_level = settings.LOG_LEVEL,
        reload    = True,   # auto-reload on file save — disable in production
    )
