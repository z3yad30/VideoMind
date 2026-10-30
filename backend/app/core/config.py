from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    project_root: Path = PROJECT_ROOT
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///data/videomind.db")
    sqlite_busy_timeout_ms: int = int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "5000"))
    worker_mode: str = os.getenv("WORKER_MODE", "embedded").lower()
    max_concurrent_jobs: int = int(os.getenv("MAX_CONCURRENT_JOBS", "1"))
    cookie_secure: bool = os.getenv(
        "COOKIE_SECURE", "true" if os.getenv("APP_ENV", "development").lower() == "production" else "false"
    ).lower() in {"1", "true", "yes", "on"}
    allowed_origins: tuple[str, ...] = tuple(
        origin.strip().rstrip("/")
        for origin in os.getenv(
            "ALLOWED_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173",
        ).split(",")
        if origin.strip()
    )
    login_rate_limit: str = os.getenv("LOGIN_RATE_LIMIT", "5/minute")
    register_rate_limit: str = os.getenv("REGISTER_RATE_LIMIT", "5/minute")
    ingestion_rate_limit: str = os.getenv("INGESTION_RATE_LIMIT", "10/hour")
    question_rate_limit: str = os.getenv("QUESTION_RATE_LIMIT", "30/minute")
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
    groq_model: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    asr_model: str = os.getenv("ASR_MODEL", "base")
    asr_device: str = os.getenv("ASR_DEVICE", "cpu")
    asr_compute_type: str = os.getenv("ASR_COMPUTE_TYPE", "int8")
    max_upload_bytes: int = int(os.getenv("MAX_UPLOAD_BYTES", str(2 * 1024 * 1024 * 1024)))
    max_media_duration_seconds: int = int(os.getenv("MAX_MEDIA_DURATION_SECONDS", "7200"))
    max_videos_per_user: int = int(os.getenv("MAX_VIDEOS_PER_USER", "100"))
    max_total_minutes_per_user: int = int(os.getenv("MAX_TOTAL_MINUTES_PER_USER", "6000"))
    ffmpeg_binary: str = os.getenv("FFMPEG_BINARY", "ffmpeg")
    ffprobe_binary: str = os.getenv("FFPROBE_BINARY", "ffprobe")
    subprocess_timeout_seconds: int = int(os.getenv("SUBPROCESS_TIMEOUT_SECONDS", "1800"))
    max_question_characters: int = int(os.getenv("MAX_QUESTION_CHARACTERS", "2000"))


settings = Settings()
