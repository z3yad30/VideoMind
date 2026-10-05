from datetime import datetime, timezone
import re
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ChatSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: float
    end: float
    text: str


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=128)
    role: Literal["user", "assistant", "system"]
    content: str
    timestamp: datetime = Field(default_factory=utc_now)
    sources: list[ChatSource] = Field(default_factory=list)
    timestamps: list[float] = Field(default_factory=list)
    answer_audio_ref: str | None = None


class ChatCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)
    video_id: str | None = None
    video_metadata: dict[str, Any] | None = None
    summary: dict[str, str] | None = None
    transcript: dict[str, Any] | None = None
    messages: list[ChatMessage] = Field(default_factory=list)

    @field_validator("video_id")
    @classmethod
    def validate_video_id(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            raise ValueError("Invalid video ID")
        return value


class ChatUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)
    video_id: str | None = None
    video_metadata: dict[str, Any] | None = None
    summary: dict[str, str] | None = None
    transcript: dict[str, Any] | None = None
    messages: list[ChatMessage] | None = None

    @field_validator("video_id")
    @classmethod
    def validate_video_id(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            raise ValueError("Invalid video ID")
        return value


class ChatQuestionResponse(BaseModel):
    user_message: ChatMessage
    assistant_message: ChatMessage
    sources: list[ChatSource]
    answer_audio_location: str | None = None


class ChatRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: str
    username: str
    title: str
    created_at: datetime
    updated_at: datetime
    video_id: str | None = None
    video_metadata: dict[str, Any] | None = None
    summary: dict[str, str] | None = None
    transcript: dict[str, Any] | None = None
    messages: list[ChatMessage] = Field(default_factory=list)


class ChatListItem(BaseModel):
    chat_id: str
    username: str
    title: str
    created_at: datetime
    updated_at: datetime
    video_id: str | None = None
