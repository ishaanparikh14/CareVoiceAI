"""
config.py — centralised settings for the CareVoice AI server.

All values have sensible defaults so the server starts with zero configuration.
Override any value by setting the matching environment variable or placing a
.env file in the server/ directory.

Example .env:
    HOST=0.0.0.0
    PORT=8000
    WAV_TEMP_DIR=/mnt/data/wav_temp
"""

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Pydantic BaseSettings reads values from (in order of precedence):
      1. Environment variables
      2. .env file in the working directory
      3. Default values defined here
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Server ────────────────────────────────────────────────────────────────
    HOST: str = "0.0.0.0"           # bind to all interfaces on the LAN
    PORT: int = 8000
    LOG_LEVEL: str = "info"

    # ── Storage ───────────────────────────────────────────────────────────────
    # Paths are resolved relative to this file's parent directory so the server
    # works regardless of the working directory it is launched from.
    BASE_DIR: Path = Path(__file__).parent
    STORAGE_DIR: Path = BASE_DIR / "storage"
    WAV_TEMP_DIR: Path = STORAGE_DIR / "wav_temp"
    MODELS_DIR: Path = STORAGE_DIR / "models"
    DB_PATH: Path = STORAGE_DIR / "carevoice.db"

    # ── Audio ingestion ───────────────────────────────────────────────────────
    MAX_WAV_SIZE_MB: int = 10           # reject uploads larger than this
    ALLOWED_CONTENT_TYPES: list[str] = ["audio/wav", "audio/wave", "audio/x-wav"]

    # Priority thresholds now live in priority_engine.py (tiered, multilingual).

    # ── WebSocket ─────────────────────────────────────────────────────────────
    # How long (seconds) to keep an idle nurse WebSocket connection alive
    WS_KEEPALIVE_SECONDS: int = 120

    # ── Auto-escalation ───────────────────────────────────────────────────────
    # An unacknowledged Urgent alert is bumped to Critical after this many
    # seconds so a forgotten request re-pages the nurse. Set 0 to disable.
    ESCALATE_URGENT_AFTER_SECONDS: int = 90
    # How often the escalation loop scans for stale alerts.
    ESCALATE_CHECK_INTERVAL_SECONDS: int = 20

    # ── Auto-reroute (scheduling failover) ────────────────────────────────────
    # An alert routed to a specific nurse that is not acknowledged within this
    # many seconds is automatically rerouted to the NEXT available nurse
    # assigned to that patient (the busy/unavailable one is skipped). Set 0 to
    # disable auto-reroute. Independent of the Urgent→Critical escalation above.
    REROUTE_UNACKED_AFTER_SECONDS: int = 30
    # How often the reroute loop scans for stale routed alerts.
    REROUTE_CHECK_INTERVAL_SECONDS: int = 10

    # ── Department routing (canteen / pharmacy / housekeeping) ────────────────
    # A decision layer that runs AFTER intent classification and BEFORE the nurse
    # dispatch. Nurse-bound requests keep using dispatch.py unchanged.
    #
    # These are the STARTUP defaults. Admins can flip them at runtime through
    # PATCH /admin/department/config; runtime changes are persisted in SQLite
    # (department_flags) and override these on the next start, so a department
    # switched off in an emergency stays off after a restart.
    DEPARTMENT_ROUTING_ENABLED: bool = True    # master switch (False = legacy behaviour only)
    # SHADOW MODE (default ON): the router runs and its decision is logged on the
    # alert row, but EVERY request still goes to the nurse exactly as before.
    DEPARTMENT_ROUTING_SHADOW: bool = True
    DEPT_CANTEEN_ENABLED: bool = True
    DEPT_PHARMACY_ENABLED: bool = True
    DEPT_HOUSEKEEPING_ENABLED: bool = True

    # Asymmetric confidence gate: misrouting to the nurse is cheap, routing a
    # clinical request AWAY from the nurse is expensive, so any non-nurse route
    # needs at least this much confidence.
    DEPT_MIN_CONFIDENCE: float = 0.85
    # Whisper average segment log-probability below this means the transcript is
    # unreliable; every non-nurse route is vetoed and the nurse gets the request.
    DEPT_MIN_STT_AVG_LOGPROB: float = -0.7

    # A department request nobody acknowledges within this many seconds is
    # escalated to the nurse through the existing dispatch engine.
    DEPT_ACK_SLA_SECONDS: int = 150
    # Repeat requests for the same room + department inside this window are merged
    # into one request with a repeat counter instead of creating duplicates.
    DEPT_DEDUPE_WINDOW_SECONDS: int = 120

    # ── CORS ──────────────────────────────────────────────────────────────────
    # Restrict to specific origins in production for security.
    # Defaults to empty list, which the server interprets as ["*"] (allow all)
    # with a warning. For production, explicitly set allowed origins.
    # Example for development: ["http://localhost:3000", "http://192.168.1.100:8000"]
    # Example for production: ["https://your-domain.com"]
    # Use ["*"] to explicitly allow all origins (not recommended for production)
    CORS_ORIGINS: list[str] = []   # Empty = allow all with warning

    # ── Pipeline stub flag ────────────────────────────────────────────────────
    # When True the server uses pipeline_stub.py (no real ML).
    # Defaults to False (real pipeline). Set USE_STUB=true in .env only for
    # local development without ML models installed.
    USE_STUB: bool = False

    # ── PostgreSQL ────────────────────────────────────────────────────────────
    # Used for users and patients tables (auth + ward management).
    # SQLite (DB_PATH above) is still used for alerts/transcripts.
    # WARNING: These are development defaults. In production, you MUST:
    # 1. Set PG_PASSWORD via environment variable
    # 2. Use DATABASE_URL_OVERRIDE for a managed/remote Postgres
    # 3. Never commit real credentials to version control
    PG_HOST:     str = "localhost"
    PG_PORT:     int = 5432
    PG_USER:     str = "postgres"
    PG_PASSWORD: str = ""  # Must be set via PG_PASSWORD environment variable
    PG_DATABASE: str = "carevoice"

    # Full connection string override. When set (e.g. a managed/remote Postgres
    # URL), it takes precedence over the PG_* parts above.
    # Example: postgresql://user:pass@your-db-host:5432/carevoice?sslmode=require
    DATABASE_URL_OVERRIDE: str = ""

    @property
    def DATABASE_URL(self) -> str:
        """asyncpg-compatible connection string.

        Managed-Postgres URLs often carry libpq params (sslmode, channel_binding)
        that asyncpg's DSN parser rejects. We strip those query params and rely
        on ssl being handled separately (see create_pool). SSL is still enforced
        via the pool's ssl argument (see DB_REQUIRES_SSL).
        """
        if self.DATABASE_URL_OVERRIDE:
            import urllib.parse as _up
            url = self.DATABASE_URL_OVERRIDE.replace("postgresql+asyncpg://", "postgresql://")
            parts = _up.urlsplit(url)
            # Drop query params asyncpg can't parse (sslmode, channel_binding, etc.)
            clean = _up.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
            return clean
        return (
            f"postgresql://{self.PG_USER}:{self.PG_PASSWORD}"
            f"@{self.PG_HOST}:{self.PG_PORT}/{self.PG_DATABASE}"
        )

    @property
    def DB_REQUIRES_SSL(self) -> bool:
        """True when the override URL carries params indicating SSL is required."""
        u = (self.DATABASE_URL_OVERRIDE or "").lower()
        return ("sslmode=require" in u) or ("channel_binding" in u)

    @property
    def DATABASE_URL_SYNC(self) -> str:
        """psycopg2-compatible connection string (used by seed script)."""
        return (
            f"postgresql+psycopg2://{self.PG_USER}:{self.PG_PASSWORD}"
            f"@{self.PG_HOST}:{self.PG_PORT}/{self.PG_DATABASE}"
        )

    # ── JWT auth ───────────────────────────────────────────────────────────────
    # CRITICAL: These must be set via environment variables in production.
    # Generate secure keys with:  python -c "import secrets; print(secrets.token_hex(32))"
    # WARNING: Leaving these empty will cause authentication to fail
    SECRET_KEY:          str = ""  # Must be set via SECRET_KEY environment variable
    JWT_ALGORITHM:       str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480   # 8 hours — one nurse shift

    # ── Registration ──────────────────────────────────────────────────────────
    # Nurses must supply this key when registering to prevent unauthorised signups.
    # CRITICAL: This must be set via environment variable in production.
    # WARNING: Leaving this empty will prevent nurse registration
    NURSE_ADMIN_KEY: str = ""  # Must be set via NURSE_ADMIN_KEY environment variable

    def model_post_init(self, __context):
        """Validate configuration and warn about insecure defaults."""
        import logging
        import warnings
        
        logger = logging.getLogger(__name__)
        
        # Check for critical security settings
        critical_warnings = []
        
        if not self.SECRET_KEY:
            critical_warnings.append("SECRET_KEY is not set. JWT authentication will fail.")
        
        if not self.NURSE_ADMIN_KEY:
            critical_warnings.append("NURSE_ADMIN_KEY is not set. Nurse registration will be disabled.")
        
        if not self.PG_PASSWORD and not self.DATABASE_URL_OVERRIDE:
            critical_warnings.append("PG_PASSWORD is not set and DATABASE_URL_OVERRIDE is empty. Database connections may fail.")
        
        if not self.CORS_ORIGINS:
            critical_warnings.append("CORS_ORIGINS is empty. The server will allow all origins with a warning. Set specific origins for production security.")
        
        # Log warnings
        if critical_warnings:
            logger.warning("⚠️  SECURITY CONFIGURATION WARNINGS:")
            for warning in critical_warnings:
                logger.warning(f"   • {warning}")
            logger.warning("   Set environment variables or edit .env file to fix these issues.")
            logger.warning("   See .env.example for required configuration.")
        
        # Check if using default insecure values (backward compatibility check)
        insecure_defaults = [
            ("postgres", self.PG_PASSWORD, "PG_PASSWORD"),
        ]
        
        for default_value, current_value, setting_name in insecure_defaults:
            if current_value == default_value:
                logger.warning(f"⚠️  {setting_name} is using default value '{default_value}'. Change this in production!")


# Module-level singleton — import this everywhere:
#   from config import settings
settings = Settings()

# Ensure storage directories exist at import time so no route needs to create them.
settings.WAV_TEMP_DIR.mkdir(parents=True, exist_ok=True)
settings.MODELS_DIR.mkdir(parents=True, exist_ok=True)
