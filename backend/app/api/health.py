import shutil
import tempfile

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from backend.app.core.config import settings
from backend.app.db.database import get_database

router = APIRouter(tags=["health"])


@router.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
def readiness_check() -> JSONResponse:
    checks: dict[str, bool] = {}
    try:
        with get_database().engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        checks["database"] = True
    except SQLAlchemyError:
        checks["database"] = False

    checks["ffmpeg"] = shutil.which(settings.ffmpeg_binary) is not None
    data_directory = settings.project_root / "data"
    try:
        with tempfile.NamedTemporaryFile(dir=data_directory, prefix=".readiness-", delete=True):
            pass
        checks["data_dir_writable"] = True
    except OSError:
        checks["data_dir_writable"] = False
    checks["groq_api_key_configured"] = bool(settings.groq_api_key.strip())
    status_code = 200 if all(checks.values()) else 503
    return JSONResponse(
        status_code=status_code,
        content={"status": "ok" if status_code == 200 else "not_ready", "checks": checks},
    )
