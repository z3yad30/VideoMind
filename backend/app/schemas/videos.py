from datetime import datetime
from typing import Literal

from pydantic import BaseModel, HttpUrl


VideoStatus = Literal["queued", "downloading", "extracting_audio", "transcribing", "completed", "failed"]


class VideoJobResponse(BaseModel):
    video_id: str
    status: VideoStatus


class VideoStatusResponse(VideoJobResponse):
    error: str | None = None
    updated_at: datetime


class YouTubeRequest(BaseModel):
    url: HttpUrl


class TranscriptSegment(BaseModel):
    text: str
    start: float
    end: float


class TranscriptResponse(BaseModel):
    video_id: str
    segments: list[TranscriptSegment]