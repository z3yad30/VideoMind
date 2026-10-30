import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from backend.app.api.auth import router as auth_router
from backend.app.api.chats import router as chats_router
from backend.app.api.health import router as health_router
from backend.app.api.videos import router as videos_router
from backend.app.core.config import settings
from backend.app.core.logging_config import configure_logging
from backend.app.core.csrf import is_allowed_request_origin
from backend.app.container import get_container
from backend.app.services.auth import auth_service
from backend.app.services.user_storage import UserStoreError
from backend.worker import WorkerRunner

configure_logging()

@asynccontextmanager
async def lifespan(_app: FastAPI):
    runner = None
    purge_task = asyncio.create_task(_purge_expired_sessions())
    try:
        if settings.worker_mode == "embedded":
            container = get_container()
            runner = WorkerRunner(container.job_queue, container.video_processing)
            runner.start()
        elif settings.worker_mode != "external":
            raise ValueError("WORKER_MODE must be 'embedded' or 'external'")
        yield
    finally:
        if runner is not None:
            await asyncio.to_thread(runner.stop)
        purge_task.cancel()
        try:
            await purge_task
        except asyncio.CancelledError:
            pass


async def _purge_expired_sessions() -> None:
    while True:
        try:
            auth_service.user_store.purge_expired_sessions()
        except UserStoreError:
            logging.getLogger(__name__).exception("Failed to purge expired sessions")
        await asyncio.sleep(60 * 60)


app = FastAPI(title="AI Video Assistant", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def enforce_csrf_origin(request: Request, call_next):
    if request.method in {"POST", "PUT", "DELETE"} and not is_allowed_request_origin(
        request.headers.get("origin"), request.headers.get("referer")
    ):
        return JSONResponse(status_code=403, content={"detail": "Request origin is not allowed"})
    return await call_next(request)


app.include_router(auth_router)
app.include_router(chats_router)
app.include_router(health_router)
app.include_router(videos_router)


@app.get("/")
def root() -> dict[str, str]:
	return {"name": "VideoMind API", "status": "ok", "health": "/health"}
