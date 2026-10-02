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

    # ── Urgent → Critical escalation ─────────────────────────────────────────
    # An Urgent alert that has not been marked ATTENDED within this many
    # seconds of its original creation time is automatically escalated to
    # Critical by escalation_manager.py. The timer is backend-authoritative
    # and starts at alert creation (NOT at ACK). Override in tests with a
    # short value (e.g. 1) so automated tests don't have to sleep 5 minutes.
    URGENT_ESCALATION_TIMEOUT_SECONDS: int = 30 #intial values was 300

    # How often (seconds) the background escalation worker scans for expired
    # Urgent alerts. Keep well below URGENT_ESCALATION_TIMEOUT_SECONDS so the
    # escalation fires promptly; tests can lower both values together.
    ESCALATION_CHECK_INTERVAL_SECONDS: int = 3  # reduced from 5s → faster escalation propagation

    # ── CORS ──────────────────────────────────────────────────────────────────
    # Restrict to LAN origins; add nurse-dashboard origin here if it's a web app
    CORS_ORIGINS: list[str] = ["*"]   # tighten to specific IP(s) in production

    # ── Pipeline stub flag ────────────────────────────────────────────────────
    # When True the server uses pipeline_stub.py (no real ML).
    # Set USE_STUB=false once Layer 3 models are integrated.
    USE_STUB: bool = True

    # ── PostgreSQL ────────────────────────────────────────────────────────────
    # Used for users and patients tables (auth + ward management).
    # SQLite (DB_PATH above) is still used for alerts/transcripts.
    # Override PG_PASSWORD via env var / .env — never commit real credentials.
    PG_HOST:     str = "localhost"
    PG_PORT:     int = 5432
    PG_USER:     str = "postgres"
    PG_PASSWORD: str = "postgres"
    PG_DATABASE: str = "carevoice"

    @property
    def DATABASE_URL(self) -> str:
        """asyncpg-compatible connection string."""
        return (
            f"postgresql://{self.PG_USER}:{self.PG_PASSWORD}"
            f"@{self.PG_HOST}:{self.PG_PORT}/{self.PG_DATABASE}"
        )

    @property
    def DATABASE_URL_SYNC(self) -> str:
        """psycopg2-compatible connection string (used by seed script)."""
        return (
            f"postgresql+psycopg2://{self.PG_USER}:{self.PG_PASSWORD}"
            f"@{self.PG_HOST}:{self.PG_PORT}/{self.PG_DATABASE}"
        )


    # ── Local LLM response layer ─────────────────────────────────────────────
    # The LLM only generates wording after the deterministic safety gate has
    # approved the request. Keep this disabled until a local model is ready.
    LLM_ENABLED: bool = False
    LLM_BASE_URL: str = "http://127.0.0.1:11434/v1"
    LLM_MODEL: str = "llama3.1:8b"
    LLM_API_KEY: str = "ollama"
    LLM_TIMEOUT_SECONDS: float = 15.0

    # ── JWT auth ───────────────────────────────────────────────────────────────
    # SECRET_KEY: change this in production — must be a long random string.
    # Generate one with:  python -c "import secrets; print(secrets.token_hex(32))"
    SECRET_KEY:          str = "carevoice-dev-secret-change-in-production-a3f9b2c1"
    JWT_ALGORITHM:       str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480   # 8 hours — one nurse shift

    # ── Registration ──────────────────────────────────────────────────────────
    # Nurses must supply this key when registering to prevent unauthorised signups.
    # Change this in production via the NURSE_ADMIN_KEY env variable.
    NURSE_ADMIN_KEY: str = "carevoice-nurse-2024"


# Module-level singleton — import this everywhere:
#   from config import settings
settings = Settings()

# Ensure storage directories exist at import time so no route needs to create them.
settings.WAV_TEMP_DIR.mkdir(parents=True, exist_ok=True)
settings.MODELS_DIR.mkdir(parents=True, exist_ok=True)
