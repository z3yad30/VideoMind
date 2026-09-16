from fastapi import FastAPI

from backend.app.api.health import router as health_router
from backend.app.api.videos import router as videos_router
from backend.app.core.logging_config import configure_logging

configure_logging()

app = FastAPI(title="AI Video Assistant", version="0.1.0")
app.include_router(health_router)
app.include_router(videos_router)


@app.get("/")
def root() -> dict[str, str]:
	return {"name": "VideoMind API", "status": "ok", "health": "/health"}
