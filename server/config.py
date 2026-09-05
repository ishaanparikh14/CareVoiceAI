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
