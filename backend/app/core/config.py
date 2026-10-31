from pathlib import Path
from typing import Annotated, ClassVar

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Application configuration loaded from environment variables and the project .env."""

    project_root: ClassVar[Path] = PROJECT_ROOT

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
    )

    database_url: str = "sqlite:///data/videomind.db"
    sqlite_busy_timeout_ms: int = 5000
    worker_mode: str = "embedded"
    max_concurrent_jobs: int = 1
    app_env: str = "development"
    cookie_secure: bool | None = None
    allowed_origins: Annotated[tuple[str, ...], NoDecode] = (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    )
    login_rate_limit: str = "5/minute"
    register_rate_limit: str = "5/minute"
    ingestion_rate_limit: str = "10/hour"
    question_rate_limit: str = "30/minute"
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    query_rewrite: bool = False
    hybrid: bool = False
    rerank: bool = False
    rerank_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    log_level: str = "INFO"
    asr_model: str = "base"
    asr_device: str = "cpu"
    asr_compute_type: str = "int8"
    max_upload_bytes: int = 2 * 1024 * 1024 * 1024
    max_media_duration_seconds: int = 7200
    max_videos_per_user: int = 100
    max_total_minutes_per_user: int = 6000
    ffmpeg_binary: str = "ffmpeg"
    ffprobe_binary: str = "ffprobe"
    subprocess_timeout_seconds: int = 1800
    max_question_characters: int = 2000

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def parse_allowed_origins(cls, value: str | tuple[str, ...] | list[str]) -> tuple[str, ...]:
        if isinstance(value, str):
            origins = value.split(",")
        else:
            origins = value
        return tuple(origin.strip().rstrip("/") for origin in origins if origin.strip())

    @model_validator(mode="after")
    def set_cookie_security_default(self) -> "Settings":
        if self.cookie_secure is None:
            self.cookie_secure = self.app_env.casefold() == "production"
        self.worker_mode = self.worker_mode.casefold()
        return self

settings = Settings()
