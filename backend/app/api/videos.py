from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile, status

from backend.app.core.config import settings
from backend.app.schemas.videos import TranscriptResponse, VideoJobResponse, VideoStatusResponse, YouTubeRequest
from backend.app.services.media import MediaProcessingError, MediaService
from backend.app.services.video_processing import VideoProcessingService

router = APIRouter(prefix="/videos", tags=["videos"])
media_service = MediaService()
video_service = VideoProcessingService(media=media_service)


@router.post("/upload", response_model=VideoJobResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_video(background_tasks: BackgroundTasks, file: UploadFile = File(...)) -> VideoJobResponse:
    try:
        suffix = media_service.validate_filename(file.filename)
        video_id = video_service.create_job()
        source_path = settings.project_root / "data" / "videos" / f"{video_id}{suffix}"
        media_service.save_upload(file, source_path)
    except MediaProcessingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await file.close()
    background_tasks.add_task(video_service.process, video_id, source_path)
    return VideoJobResponse(video_id=video_id, status="queued")


@router.post("/youtube", response_model=VideoJobResponse, status_code=status.HTTP_202_ACCEPTED)
async def process_youtube(request: YouTubeRequest, background_tasks: BackgroundTasks) -> VideoJobResponse:
    video_id = video_service.create_job()
    background_tasks.add_task(video_service.process, video_id, Path(), str(request.url))
    return VideoJobResponse(video_id=video_id, status="queued")


@router.get("/{video_id}/status", response_model=VideoStatusResponse)
async def get_video_status(video_id: str) -> VideoStatusResponse:
    job = video_service.get_job(video_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Video not found")
    return VideoStatusResponse(video_id=job.video_id, status=job.status, error=job.error, updated_at=job.updated_at)


@router.get("/{video_id}/transcript", response_model=TranscriptResponse)
async def get_transcript(video_id: str) -> TranscriptResponse:
    if video_service.get_job(video_id) is None:
        raise HTTPException(status_code=404, detail="Video not found")
    segments = video_service.get_transcript(video_id)
    if segments is None:
        raise HTTPException(status_code=409, detail="Transcript is not ready")
    return TranscriptResponse(video_id=video_id, segments=segments)