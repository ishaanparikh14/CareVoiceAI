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

import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import escalation_manager
from config import settings
from pg_database import create_pool, close_pool, init_pg_db
from models import HealthResponse
from routers import audio as audio_router
from routers import alerts as alerts_router
from routers import ws as ws_router
from routers import auth_router
from routers import patient_ws as patient_ws_router
from routers import voice_notes as voice_notes_router
from routers import admin as admin_router

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level   = settings.LOG_LEVEL.upper(),
    format  = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt = "%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Runs once at startup (before the first request) and once at shutdown.

    Startup:
      • Initialise the PostgreSQL schema (idempotent migrations / seeds).
      • Wire the WebSocket broadcaster into the audio router.
      • Log the active configuration.

    Shutdown:
      • PostgreSQL pool is closed on shutdown.
    """
    # ── Startup ───────────────────────────────────────────────────────────────
    logger.info("CareVoice AI server starting up")
    logger.info("  Host        : %s:%d", settings.HOST, settings.PORT)
    logger.info("  PostgreSQL  : %s:%d/%s", settings.PG_HOST, settings.PG_PORT, settings.PG_DATABASE)
    logger.info("  WAV temp dir: %s",    settings.WAV_TEMP_DIR)
    logger.info("  Pipeline    : %s",    "STUB" if settings.USE_STUB else "REAL")

    # PostgreSQL — users, patients, alerts, voice notes and EMR
    await create_pool()
    await init_pg_db()

    # Inject nurse broadcaster into both the HTTP audio router AND the patient WS router
    audio_router.router
    from routers.audio import set_broadcaster
    from routers.patient_ws import set_nurse_broadcaster
    set_broadcaster(ws_router.manager.broadcast)
    set_nurse_broadcaster(ws_router.manager.broadcast)
    escalation_manager.set_broadcaster(ws_router.manager.broadcast)
    logger.info("WebSocket broadcaster wired to audio router, patient WS router, and escalation manager")

    # Start the Urgent → Critical escalation background worker. It reuses the
    # SAME WebSocket ConnectionManager wired in above — no separate transport.
    escalation_task = asyncio.create_task(escalation_manager.run_forever())

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
    try:
        await escalation_task
    except asyncio.CancelledError:
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
# Default allows all origins so Android apps on any ward IP can reach the server.
# Tighten CORS_ORIGINS in .env (e.g. ["http://192.168.1.0/24"]) for production.
app.add_middleware(
    CORSMiddleware,
    allow_origins     = settings.CORS_ORIGINS,
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
app.include_router(voice_notes_router.router)   # POST/GET /voice-notes

# ── Static pages (registered BEFORE admin API router so /admin exact match ──
# serves the HTML page while /admin/stats etc. route into the API)
_static_dir = settings.BASE_DIR / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

@app.get("/dashboard", include_in_schema=False)
async def dashboard():
    """Serve the main app dashboard (requires auth — JS handles redirect)."""
    return FileResponse(str(_static_dir / "index.html"))

@app.get("/login", include_in_schema=False)
async def login_page():
    """Serve the login page."""
    return FileResponse(str(_static_dir / "login.html"))

@app.get("/admin", include_in_schema=False)
async def admin_page():
    """Serve the admin monitoring dashboard (requires admin role — JS handles guard)."""
    return FileResponse(str(_static_dir / "admin.html"))

@app.get("/test", include_in_schema=False)
async def test_page():
    """Microphone + WebSocket diagnostic page."""
    return FileResponse(str(_static_dir / "test_mic.html"))

@app.get("/", include_in_schema=False)
async def root():
    """Root redirects to login."""
    return FileResponse(str(_static_dir / "login.html"))

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
        db_path  = "PostgreSQL",
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
