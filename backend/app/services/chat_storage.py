import json
import os
from pathlib import Path
import re
import tempfile
from threading import RLock
from uuid import uuid4

from pydantic import ValidationError

from backend.app.core.config import settings
from backend.app.schemas.chats import ChatCreate, ChatRecord, ChatUpdate, utc_now


CHAT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9._-]{1,32}$")


class ChatStoreError(Exception):
    pass


class ChatNotFoundError(Exception):
    pass


class InvalidChatIdError(Exception):
    pass


class ChatStore:
    def __init__(self, root: Path | None = None, data_root: Path | None = None) -> None:
        self.root = root or settings.project_root / "data" / "chats"
        self.data_root = data_root or self.root.parent
        self._lock = RLock()

    def create(self, username: str, request: ChatCreate) -> ChatRecord:
        user_dir = self._user_dir(username)
        now = utc_now()
        messages = request.messages
        title = (request.title or "").strip()
        if not title:
            title = next((message.content.strip()[:80] for message in messages if message.role == "user" and message.content.strip()), "New chat")
        with self._lock:
            chat_id = uuid4().hex
            while (user_dir / f"{chat_id}.json").exists():
                chat_id = uuid4().hex
            record = ChatRecord(
                chat_id=chat_id,
                username=username,
                title=title,
                created_at=now,
                updated_at=now,
                video_id=request.video_id,
                video_metadata=request.video_metadata,
                summary=request.summary,
                transcript=request.transcript,
                messages=messages,
            )
            self._write(record)
            return record

    def list_for_user(self, username: str) -> list[ChatRecord]:
        user_dir = self._user_dir(username)
        with self._lock:
            chats = [self._read(path, username) for path in user_dir.glob("*.json") if CHAT_ID_PATTERN.fullmatch(path.stem)]
        return sorted(chats, key=lambda chat: chat.updated_at, reverse=True)

    def get(self, username: str, chat_id: str) -> ChatRecord:
        path = self._chat_path(username, chat_id)
        with self._lock:
            record = self._read(path, username)
        return self._load_video_context(record)

    def update(self, username: str, chat_id: str, request: ChatUpdate) -> ChatRecord:
        path = self._chat_path(username, chat_id)
        changes = request.model_dump(exclude_unset=True)
        with self._lock:
            record = self._read(path, username)
            if "video_id" in changes and changes["video_id"] != record.video_id:
                changes.setdefault("summary", None)
                changes.setdefault("transcript", None)
                changes.setdefault("video_metadata", None)
            if "title" in changes:
                changes["title"] = (changes["title"] or "").strip() or "New chat"
            changes["updated_at"] = utc_now()
            updated = ChatRecord.model_validate({**record.model_dump(), **changes})
            self._write(updated)
        return self._load_video_context(updated)

    def delete(self, username: str, chat_id: str) -> None:
        path = self._chat_path(username, chat_id)
        with self._lock:
            self._read(path, username)
            try:
                path.unlink()
            except OSError as exc:
                raise ChatStoreError("Chat storage is unavailable") from exc

    def _user_dir(self, username: str) -> Path:
        if not USERNAME_PATTERN.fullmatch(username):
            raise ChatStoreError("Chat storage is unavailable")
        try:
            if self.root.is_symlink():
                raise ChatStoreError("Chat storage is unavailable")
            self.root.mkdir(parents=True, exist_ok=True)
            root = self.root.resolve()
            user_dir = self.root / username
            if user_dir.is_symlink():
                raise ChatStoreError("Chat storage is unavailable")
            user_dir.mkdir(parents=True, exist_ok=True)
            if user_dir.resolve().parent != root:
                raise ChatStoreError("Chat storage is unavailable")
            return user_dir
        except OSError as exc:
            raise ChatStoreError("Chat storage is unavailable") from exc

    def _chat_path(self, username: str, chat_id: str) -> Path:
        if not CHAT_ID_PATTERN.fullmatch(chat_id):
            raise InvalidChatIdError
        return self._user_dir(username) / f"{chat_id}.json"

    def _read(self, path: Path, username: str) -> ChatRecord:
        if path.is_symlink():
            raise ChatStoreError("Chat storage is unavailable")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            record = ChatRecord.model_validate(payload)
            if record.username != username or path.stem != record.chat_id:
                raise ValueError("Chat ownership or identifier mismatch")
            return record
        except FileNotFoundError as exc:
            raise ChatNotFoundError from exc
        except (OSError, UnicodeError, json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
            raise ChatStoreError("Chat storage is unavailable") from exc

    def _write(self, record: ChatRecord) -> None:
        path = self._chat_path(record.username, record.chat_id)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{record.chat_id}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_name = temporary_file.name
                json.dump(record.model_dump(mode="json"), temporary_file, ensure_ascii=True, indent=2)
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_name, path)
        except OSError as exc:
            raise ChatStoreError("Chat storage is unavailable") from exc
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)

    def _load_video_context(self, record: ChatRecord) -> ChatRecord:
        if not record.video_id:
            return record
        summary = record.summary or self._read_artifact("summaries", record.video_id, "summary")
        transcript = record.transcript or self._read_artifact("transcripts", record.video_id)
        return record.model_copy(update={"summary": summary, "transcript": transcript})

    def _read_artifact(self, directory: str, video_id: str, key: str | None = None):
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


__all__ = ["ChatNotFoundError", "ChatStore", "ChatStoreError", "InvalidChatIdError", "chat_store"]