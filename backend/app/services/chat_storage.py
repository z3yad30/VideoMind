import json
from pathlib import Path

from pydantic import ValidationError

from backend.app.core.config import settings
from backend.app.db.database import get_database
from backend.app.repositories.chat import (
    CHAT_ID_PATTERN,
    ChatNotFoundError,
    ChatRepo,
    ChatRepoError,
    ChatStoreError,
    InvalidChatIdError,
)
from backend.app.schemas.chats import ChatCreate, ChatMessage, ChatRecord, ChatUpdate


class ChatStore:
    def __init__(
        self,
        repository: ChatRepo | Path | None = None,
        data_root: Path | None = None,
    ) -> None:
        if isinstance(repository, Path):
            raise TypeError("ChatStore requires a ChatRepo, not a JSON directory")
        self.repository = repository or ChatRepo(get_database().session_factory)
        self.data_root = data_root or settings.project_root / "data"

    def create(self, username: str, request: ChatCreate) -> ChatRecord:
        try:
            return self.repository.create(username, request)
        except (ChatNotFoundError, InvalidChatIdError):
            raise
        except ChatRepoError as exc:
            raise ChatStoreError("Chat storage is unavailable") from exc

    def list_for_user(self, username: str) -> list[ChatRecord]:
        try:
            return self.repository.list_for_user(username)
        except (ChatNotFoundError, InvalidChatIdError):
            raise
        except ChatRepoError as exc:
            raise ChatStoreError("Chat storage is unavailable") from exc

    def get(self, username: str, chat_id: str) -> ChatRecord:
        try:
            record = self.repository.get(username, chat_id)
        except (ChatNotFoundError, InvalidChatIdError):
            raise
        except ChatRepoError as exc:
            raise ChatStoreError("Chat storage is unavailable") from exc
        return self._load_video_context(record)

    def update(self, username: str, chat_id: str, request: ChatUpdate) -> ChatRecord:
        try:
            record = self.repository.update(username, chat_id, request)
        except (ChatNotFoundError, InvalidChatIdError):
            raise
        except ChatRepoError as exc:
            raise ChatStoreError("Chat storage is unavailable") from exc
        return self._load_video_context(record)

    def append_message(self, username: str, chat_id: str, message: ChatMessage) -> ChatRecord:
        try:
            return self.repository.append_message(username, chat_id, message)
        except (ChatNotFoundError, InvalidChatIdError):
            raise
        except ChatRepoError as exc:
            raise ChatStoreError("Chat storage is unavailable") from exc

    def set_message_audio_ref(
        self, username: str, chat_id: str, message_id: str, audio_ref: str
    ) -> ChatMessage | None:
        try:
            return self.repository.set_message_audio_ref(username, chat_id, message_id, audio_ref)
        except (ChatNotFoundError, InvalidChatIdError):
            raise
        except ChatRepoError as exc:
            raise ChatStoreError("Chat storage is unavailable") from exc

    def delete(self, username: str, chat_id: str) -> None:
        try:
            self.repository.delete(username, chat_id)
        except (ChatNotFoundError, InvalidChatIdError):
            raise
        except ChatRepoError as exc:
            raise ChatStoreError("Chat storage is unavailable") from exc

    def _load_video_context(self, record: ChatRecord) -> ChatRecord:
        if not record.video_id:
            return record
        try:
            asset_id = self.repository.asset_id_for_video(record.username, record.video_id)
        except ChatRepoError as exc:
            raise ChatStoreError("Chat storage is unavailable") from exc
        artifact_id = asset_id or record.video_id
        summary = record.summary or self._read_artifact("summaries", artifact_id, "summary")
        transcript = record.transcript or self._read_artifact("transcripts", artifact_id)
        return record.model_copy(update={"summary": summary, "transcript": transcript})

    def _read_artifact(self, directory: str, video_id: str, key: str | None = None):
        if not CHAT_ID_PATTERN.fullmatch(video_id):
            return None
        path = self.data_root / directory / f"{video_id}.json"
        if path.is_symlink():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            value = payload.get(key) if key else payload
            return value if isinstance(value, dict) else None
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
            return None


chat_store = ChatStore()


__all__ = [
    "ChatNotFoundError",
    "ChatStore",
    "ChatStoreError",
    "InvalidChatIdError",
    "chat_store",
]
