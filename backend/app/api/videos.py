import asyncio
import json
import logging
import mimetypes
from pathlib import Path
import re

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse

from backend.app.ports import BlobStore, VectorStore

from backend.app.api.dependencies import get_current_user
from backend.app.container import (
    get_ai_service,
    get_blob_store,
    get_media_service,
    get_video_processing_service,
    get_vector_store,
    get_voice_service,
)
from backend.app.core.config import settings
from backend.app.core.rate_limit import rate_limiter
from backend.app.schemas.videos import (
    QuestionRequest,
    QuestionResponse,
    SummaryResponse,
    TranscriptResponse,
    VideoJobResponse,
    VideoStatusResponse,
    ProcessingEventResponse,
    VoiceQuestionResponse,
    YouTubeRequest,
)
from backend.app.services.llm import VideoAIService
from backend.app.services.media import MediaProcessingError, MediaService
from backend.app.services.video_processing import VideoProcessingService
from backend.app.services.voice import VoiceQuestionService
from backend.app.services.auth import AuthenticatedUser
from backend.app.services.asset_cleanup import AssetCleanupError, delete_asset_artifacts
from backend.app.services.video_storage import VideoOwnershipError, video_ownership

router = APIRouter(prefix="/videos", tags=["videos"])
logger = logging.getLogger(__name__)
_ARTIFACT_ID = re.compile(r"^[0-9a-f]{32}$")


def _require_video_owner(video_id: str, user: AuthenticatedUser) -> None:
    try:
        owned = video_ownership.is_owned_by(video_id, user.username)
    except VideoOwnershipError as exc:
        raise HTTPException(status_code=503, detail="Video storage is unavailable") from exc
    if not owned:
        raise HTTPException(status_code=404, detail="Video not found")


def _register_video_owner(
    video_id: str,
    username: str,
    source_type: str = "upload",
    source_url: str | None = None,
    duration_s: float | None = None,
) -> None:
    try:
        video_ownership.register(video_id, username, source_type, source_url, duration_s)
    except VideoOwnershipError as exc:
        raise HTTPException(status_code=503, detail="Video storage is unavailable") from exc


def _start_processing(video_service: VideoProcessingService, video_id: str) -> None:
    video_service.enqueue(video_id)


def _resolve_source_media_path(
    video_id: str,
    blob_store: BlobStore,
    blob_key: str | None = None,
) -> Path:
    if not _ARTIFACT_ID.fullmatch(video_id):
        raise HTTPException(status_code=404, detail="Video not found")
    if blob_key:
        asset_path = blob_store.open(blob_key)
        if asset_path.is_file():
            return asset_path
    videos_dir = blob_store.open(f"videos/{video_id}").parent
    matches = sorted(path for path in videos_dir.glob(f"{video_id}.*") if path.is_file())
    for candidate in matches:
        if candidate.suffix.lower() in MediaService.supported_extensions:
            return candidate
    raise HTTPException(status_code=404, detail="Video not found")


def _asset_id_for_video(video_id: str, username: str) -> str:
    try:
        return video_ownership.asset_id_for_video(video_id, username) or video_id
    except VideoOwnershipError as exc:
        raise HTTPException(status_code=503, detail="Video storage is unavailable") from exc


@router.post("/upload", response_model=VideoJobResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_video(
    file: UploadFile = File(...),
    user: AuthenticatedUser = Depends(get_current_user),
    video_service: VideoProcessingService = Depends(get_video_processing_service),
    media_service: MediaService = Depends(get_media_service),
    blob_store: BlobStore = Depends(get_blob_store),
) -> VideoJobResponse:
    source_path: Path | None = None
    should_enqueue = False
    try:
        _check_user_rate_limit(user.username, settings.ingestion_rate_limit)
        suffix = media_service.validate_filename(file.filename)
        video_id = video_service.create_job()
        source_path = blob_store.open(f"videos/incoming/{video_id}{suffix}")
        fingerprint = media_service.save_upload_with_digest(file, source_path)
        duration_s = media_service.get_duration(source_path)
        _check_ingestion_quota(user.username, duration_s)
        try:
            registration = video_ownership.register_upload(
                video_id,
                user.username,
                fingerprint.sha256,
                fingerprint.size,
                duration_s,
                suffix,
            )
        except VideoOwnershipError as exc:
            raise HTTPException(status_code=503, detail="Video storage is unavailable") from exc
        if not registration.duplicate:
            try:
                blob_store.put(registration.blob_key, source_path)
            except Exception as exc:
                try:
                    cleanup_assets = video_ownership.delete_video(video_id, user.username)
                    for asset in cleanup_assets:
                        blob_store.delete(asset.blob_key)
                except Exception:
                    logger.exception("Failed to roll back incomplete upload for video %s", video_id)
                raise HTTPException(status_code=503, detail="Unable to store uploaded media") from exc
        if registration.completed:
            video_service.mark_completed(video_id)
            return VideoJobResponse(video_id=video_id, status="completed")
        should_enqueue = registration.enqueue
        if should_enqueue:
            _start_processing(video_service, video_id)
    except MediaProcessingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    finally:
        await file.close()
        if source_path is not None:
            try:
                blob_store.delete(f"videos/incoming/{video_id}{suffix}")
            except Exception as exc:
                logger.exception("Failed to remove temporary upload for video %s", video_id)
                raise HTTPException(status_code=503, detail="Unable to clean up temporary upload") from exc
    return VideoJobResponse(video_id=video_id, status="queued")


@router.post("/youtube", response_model=VideoJobResponse, status_code=status.HTTP_202_ACCEPTED)
async def process_youtube(
    request: YouTubeRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    video_service: VideoProcessingService = Depends(get_video_processing_service),
    media_service: MediaService = Depends(get_media_service),
) -> VideoJobResponse:
    _check_user_rate_limit(user.username, settings.ingestion_rate_limit)
    try:
        duration_s = media_service.get_youtube_duration(str(request.url))
        _check_ingestion_quota(user.username, duration_s)
    except MediaProcessingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    video_id = video_service.create_job()
    _register_video_owner(video_id, user.username, "youtube", str(request.url), duration_s)
    _start_processing(video_service, video_id)
    return VideoJobResponse(video_id=video_id, status="queued")


@router.get("/{video_id}", response_model=VideoJobResponse)
async def get_video(video_id: str, user: AuthenticatedUser = Depends(get_current_user), video_service: VideoProcessingService = Depends(get_video_processing_service)) -> VideoJobResponse:
    _require_video_owner(video_id, user)
    job = video_service.get_job(video_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Video not found")
    return VideoJobResponse(video_id=job.video_id, status=job.status)


@router.get("/{video_id}/status", response_model=VideoStatusResponse)
async def get_video_status(video_id: str, user: AuthenticatedUser = Depends(get_current_user), video_service: VideoProcessingService = Depends(get_video_processing_service)) -> VideoStatusResponse:
    _require_video_owner(video_id, user)
    job = video_service.get_job(video_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Video not found")
    return VideoStatusResponse(video_id=job.video_id, status=job.status, error=job.error, updated_at=job.updated_at, stages=job.stages or [])


@router.get("/{video_id}/events")
async def video_events(
    video_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    video_service: VideoProcessingService = Depends(get_video_processing_service),
) -> StreamingResponse:
    _require_video_owner(video_id, user)
    if video_service.get_job(video_id) is None:
        raise HTTPException(status_code=404, detail="Video not found")
    try:
        last_event_id = max(0, int(request.headers.get("last-event-id", "0")))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid Last-Event-ID header") from exc

    async def stream():
        cursor = last_event_id
        while True:
            events = video_service.get_events(video_id, cursor)
            for event in events:
                cursor = int(event.pop("id"))
                payload = ProcessingEventResponse(**event).model_dump(mode="json")
                yield f"id: {cursor}\nevent: {payload['event']}\ndata: {json.dumps(payload)}\n\n"
            job = video_service.get_job(video_id)
            if job and job.status in {"completed", "failed"} and not events:
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/{video_id}/transcript", response_model=TranscriptResponse)
async def get_transcript(video_id: str, user: AuthenticatedUser = Depends(get_current_user), video_service: VideoProcessingService = Depends(get_video_processing_service)) -> TranscriptResponse:
    _require_video_owner(video_id, user)
    job = video_service.get_job(video_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Video not found")
    if job.status != "completed":
        raise HTTPException(status_code=409, detail="Transcript is not ready")
    segments = video_service.get_transcript(_asset_id_for_video(video_id, user.username))
    if segments is None:
        raise HTTPException(status_code=409, detail="Transcript is not ready")
    return TranscriptResponse(video_id=video_id, segments=segments)


@router.get("/{video_id}/summary", response_model=SummaryResponse)
async def get_summary(video_id: str, user: AuthenticatedUser = Depends(get_current_user), video_service: VideoProcessingService = Depends(get_video_processing_service), ai_service: VideoAIService = Depends(get_ai_service)) -> SummaryResponse:
    _require_video_owner(video_id, user)
    job = video_service.get_job(video_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Video not found")
    if job.status != "completed":
        raise HTTPException(status_code=409, detail="Summary is not ready")
    summary = ai_service.get_summary(_asset_id_for_video(video_id, user.username))
    if summary is None:
        raise HTTPException(status_code=409, detail="Summary is not ready")
    return SummaryResponse(video_id=video_id, summary=summary)


@router.get("/{video_id}/summary/audio")
async def get_summary_audio(video_id: str, user: AuthenticatedUser = Depends(get_current_user), video_service: VideoProcessingService = Depends(get_video_processing_service), ai_service: VideoAIService = Depends(get_ai_service)) -> FileResponse:
    _require_video_owner(video_id, user)
    job = video_service.get_job(video_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Video not found")
    if job.status != "completed":
        raise HTTPException(status_code=409, detail="Summary audio is not ready")
    asset_id = _asset_id_for_video(video_id, user.username)
    if not _ARTIFACT_ID.fullmatch(asset_id):
        raise HTTPException(status_code=404, detail="Summary audio not found")
    audio_path = ai_service.get_summary_audio_path(asset_id)
    if audio_path is None:
        raise HTTPException(status_code=404, detail="Summary audio is not ready")
    return FileResponse(audio_path, media_type="audio/wav", filename=f"{video_id}-summary.wav")


@router.get("/{video_id}/media")
async def get_video_media(video_id: str, user: AuthenticatedUser = Depends(get_current_user), video_service: VideoProcessingService = Depends(get_video_processing_service), blob_store: BlobStore = Depends(get_blob_store)) -> FileResponse:
    _require_video_owner(video_id, user)
    job = video_service.get_job(video_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Video not found")
    if job.status != "completed":
        raise HTTPException(status_code=409, detail="Video is not ready")
    try:
        blob_key = video_ownership.blob_key_for_video(video_id, user.username)
    except VideoOwnershipError as exc:
        raise HTTPException(status_code=503, detail="Video storage is unavailable") from exc
    source_path = _resolve_source_media_path(video_id, blob_store, blob_key)
    media_type = mimetypes.guess_type(source_path.name)[0] or "application/octet-stream"
    return FileResponse(source_path, media_type=media_type, filename=source_path.name)


@router.post("/{video_id}/question", response_model=QuestionResponse)
async def ask_question(video_id: str, request: QuestionRequest, user: AuthenticatedUser = Depends(get_current_user), video_service: VideoProcessingService = Depends(get_video_processing_service), ai_service: VideoAIService = Depends(get_ai_service)) -> QuestionResponse:
    _check_user_rate_limit(user.username, settings.question_rate_limit)
    _require_video_owner(video_id, user)
    job = video_service.get_job(video_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Video not found")
    if job.status != "completed":
        raise HTTPException(status_code=409, detail="Video processing is not complete")
    if not request.question.strip():
        raise HTTPException(status_code=400, detail="Question must not be empty")
    try:
        asset_id = _asset_id_for_video(video_id, user.username)
        result = await run_in_threadpool(ai_service.answer_question, asset_id, request.question)
        return QuestionResponse(**result)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/{video_id}/voice-question", response_model=VoiceQuestionResponse)
async def ask_voice_question(video_id: str, file: UploadFile = File(...), user: AuthenticatedUser = Depends(get_current_user), video_service: VideoProcessingService = Depends(get_video_processing_service), voice_service: VoiceQuestionService = Depends(get_voice_service)) -> VoiceQuestionResponse:
    _check_user_rate_limit(user.username, settings.question_rate_limit)
    _require_video_owner(video_id, user)
    job = video_service.get_job(video_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Video not found")
    if job.status != "completed":
        raise HTTPException(status_code=409, detail="Video processing is not complete")
    try:
        asset_id = _asset_id_for_video(video_id, user.username)
        result = await run_in_threadpool(voice_service.answer, asset_id, file)
        return VoiceQuestionResponse(
            transcribed_question=str(result["transcribed_question"]),
            answer=str(result["answer"]),
            sources=result["sources"],
            audio_answer_location=f"/videos/{video_id}/answers/{result['answer_id']}/audio",
        )
    except (ValueError, MediaProcessingError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        await file.close()


def _check_user_rate_limit(username: str, limit: str) -> None:
    if not rate_limiter.hit(limit, f"user:{username.casefold()}"):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")


def _check_ingestion_quota(username: str, duration_s: float) -> None:
    try:
        allowed = video_ownership.check_quota(
            username,
            duration_s,
            settings.max_videos_per_user,
            settings.max_total_minutes_per_user,
        )
    except VideoOwnershipError as exc:
        raise HTTPException(status_code=503, detail="Video storage is unavailable") from exc
    if not allowed:
        raise HTTPException(status_code=429, detail="User ingestion quota exceeded")


@router.get("/{video_id}/answers/{answer_id}/audio")
async def get_voice_answer_audio(video_id: str, answer_id: str, user: AuthenticatedUser = Depends(get_current_user), video_service: VideoProcessingService = Depends(get_video_processing_service), blob_store: BlobStore = Depends(get_blob_store)) -> FileResponse:
    _require_video_owner(video_id, user)
    job = video_service.get_job(video_id)
    if job is None or job.status != "completed" or not _ARTIFACT_ID.fullmatch(video_id) or not _ARTIFACT_ID.fullmatch(answer_id):
        raise HTTPException(status_code=404, detail="Answer audio not found")
    asset_id = _asset_id_for_video(video_id, user.username)
    audio_path = blob_store.open(f"audio/answers/{asset_id}/{answer_id}.wav")
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail="Answer audio not found")
    return FileResponse(audio_path, media_type="audio/wav", filename=f"{answer_id}.wav")


@router.delete("/{video_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_video(
    video_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    blob_store: BlobStore = Depends(get_blob_store),
    vector_store: VectorStore = Depends(get_vector_store),
) -> None:
    _require_video_owner(video_id, user)
    try:
        cleanup_assets = video_ownership.delete_video(video_id, user.username)
        for asset in cleanup_assets:
            delete_asset_artifacts(asset, blob_store, vector_store)
    except VideoOwnershipError as exc:
        raise HTTPException(status_code=503, detail="Video storage is unavailable") from exc
    except AssetCleanupError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc