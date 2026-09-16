from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator

from backend.app.core.config import settings


VideoStatus = Literal["queued", "validating", "downloading", "extracting_audio", "detecting_language", "transcribing", "building_transcript", "chunking", "embedding", "indexing", "summarizing", "generating_summary_audio", "completed", "failed"]
StageStatus = Literal["pending", "running", "completed", "failed", "skipped"]


class ProcessingStageResponse(BaseModel):
    id: str
    display_name: str
    status: StageStatus
    progress: int | None = None
    message: str | None = None
    detail: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None


class ProcessingEventResponse(BaseModel):
    event: str
    video_id: str
    stage: str | None = None
    status: str
    progress: int | None = None
    message: str | None = None
    detail: str | None = None
    timestamp: datetime
    stages: list[ProcessingStageResponse] | None = None


class VideoJobResponse(BaseModel):
    video_id: str
    status: VideoStatus


class VideoStatusResponse(VideoJobResponse):
    error: str | None = None
    updated_at: datetime
    stages: list[ProcessingStageResponse] = Field(default_factory=list)


class YouTubeRequest(BaseModel):
    url: HttpUrl

    @field_validator("url")
    @classmethod
    def validate_youtube_host(cls, value: HttpUrl) -> HttpUrl:
        if value.host not in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"}:
            raise ValueError("Only YouTube URLs are supported")
        return value


class TranscriptSegment(BaseModel):
    text: str
    start: float
    end: float


class TranscriptResponse(BaseModel):
    video_id: str
    segments: list[TranscriptSegment]


class SummaryResponse(BaseModel):
    video_id: str
    summary: dict[str, str]


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=settings.max_question_characters)


class QuestionSource(BaseModel):
    start: float
    end: float
    text: str


class QuestionResponse(BaseModel):
    answer: str
    sources: list[QuestionSource]


class VoiceQuestionResponse(QuestionResponse):
    transcribed_question: str
    audio_answer_location: str