from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse

from backend.app.core.config import settings
from backend.app.schemas.videos import (
    QuestionRequest,
    QuestionResponse,
    SummaryResponse,
    TranscriptResponse,
    VideoJobResponse,
    VideoStatusResponse,
    VoiceQuestionResponse,
    YouTubeRequest,
)
from backend.app.services.llm import VideoAIService
from backend.app.services.media import MediaProcessingError, MediaService
from backend.app.services.video_processing import VideoProcessingService
from backend.app.services.voice import VoiceQuestionService

router = APIRouter(prefix="/videos", tags=["videos"])
media_service = MediaService()
ai_service = VideoAIService()
video_service = VideoProcessingService(media=media_service, ai=ai_service)
voice_service = VoiceQuestionService(ai=ai_service, media=media_service)


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


@router.get("/{video_id}/summary", response_model=SummaryResponse)
async def get_summary(video_id: str) -> SummaryResponse:
    if video_service.get_job(video_id) is None:
        raise HTTPException(status_code=404, detail="Video not found")
    summary = ai_service.get_summary(video_id)
    if summary is None:
        raise HTTPException(status_code=409, detail="Summary is not ready")
    return SummaryResponse(video_id=video_id, summary=summary)


@router.get("/{video_id}/summary/audio")
async def get_summary_audio(video_id: str) -> FileResponse:
    if video_service.get_job(video_id) is None:
        raise HTTPException(status_code=404, detail="Video not found")
    audio_path = ai_service.get_summary_audio_path(video_id)
    if audio_path is None:
        raise HTTPException(status_code=404, detail="Summary audio is not ready")
    return FileResponse(audio_path, media_type="audio/wav", filename=f"{video_id}-summary.wav")


@router.post("/{video_id}/question", response_model=QuestionResponse)
async def ask_question(video_id: str, request: QuestionRequest) -> QuestionResponse:
    job = video_service.get_job(video_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Video not found")
    if job.status != "completed":
        raise HTTPException(status_code=409, detail="Video processing is not complete")
    if not request.question.strip():
        raise HTTPException(status_code=400, detail="Question must not be empty")
    try:
        return QuestionResponse(**ai_service.answer_question(video_id, request.question))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/{video_id}/voice-question", response_model=VoiceQuestionResponse)
async def ask_voice_question(video_id: str, file: UploadFile = File(...)) -> VoiceQuestionResponse:
    job = video_service.get_job(video_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Video not found")
    if job.status != "completed":
        raise HTTPException(status_code=409, detail="Video processing is not complete")
    try:
        result = voice_service.answer(video_id, file)
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


@router.get("/{video_id}/answers/{answer_id}/audio")
async def get_voice_answer_audio(video_id: str, answer_id: str) -> FileResponse:
    audio_path = voice_service.audio_dir / video_id / f"{answer_id}.wav"
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail="Answer audio not found")
    return FileResponse(audio_path, media_type="audio/wav", filename=f"{answer_id}.wav")