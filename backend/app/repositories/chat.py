import re
from datetime import datetime, timezone
from typing import Callable
from uuid import uuid4

from sqlalchemy import case, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session as OrmSession, selectinload

from backend.app.db.models import Chat, Message, MessageSource, User
from backend.app.schemas.chats import ChatCreate, ChatMessage, ChatRecord, ChatUpdate, utc_now


CHAT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9._-]{1,32}$")


class ChatRepoError(Exception):
    pass


class ChatStoreError(ChatRepoError):
    pass


class ChatNotFoundError(ChatRepoError):
    pass


class InvalidChatIdError(ChatRepoError):
    pass


class ChatRepo:
    def __init__(self, session_factory: Callable[[], OrmSession]) -> None:
        self.session_factory = session_factory

    def create(self, username: str, request: ChatCreate) -> ChatRecord:
        now = utc_now()
        title = (request.title or "").strip()
        if not title:
            title = next(
                (message.content.strip()[:80] for message in request.messages if message.role == "user" and message.content.strip()),
                "New chat",
            )
        try:
            with self.session_factory.begin() as session:
                user = session.scalar(select(User).where(User.username == username.casefold()))
                if user is None:
                    raise ChatStoreError("Chat storage is unavailable")
                chat_id = uuid4().hex
                chat = Chat(
                    id=chat_id,
                    user_id=user.id,
                    video_id=request.video_id,
                    title=title,
                    created_at=now,
                    updated_at=now,
                    video_metadata=request.video_metadata,
                    summary=request.summary,
                    transcript=request.transcript,
                )
                session.add(chat)
                self._replace_messages(session, chat, request.messages)
                session.flush()
                record = self._to_record(chat, username)
            return record
        except IntegrityError as exc:
            raise ChatStoreError("Chat storage is unavailable") from exc
        except SQLAlchemyError as exc:
            raise ChatStoreError("Chat storage is unavailable") from exc

    def list_for_user(self, username: str) -> list[ChatRecord]:
        try:
            with self.session_factory() as session:
                chats = session.scalars(
                    select(Chat)
                    .join(User)
                    .where(User.username == username.casefold())
                    .options(selectinload(Chat.messages).selectinload(Message.sources))
                    .order_by(Chat.updated_at.desc())
                ).all()
                return [self._to_record(chat, username) for chat in chats]
        except SQLAlchemyError as exc:
            raise ChatStoreError("Chat storage is unavailable") from exc

    def get(self, username: str, chat_id: str) -> ChatRecord:
        chat = self._fetch(username, chat_id)
        return self._to_record(chat, username)

    def update(self, username: str, chat_id: str, request: ChatUpdate) -> ChatRecord:
        changes = request.model_dump(exclude_unset=True)
        now = utc_now()
        try:
            with self.session_factory.begin() as session:
                chat = self._get_in_session(session, username, chat_id, lock=True)
                if "video_id" in changes and changes["video_id"] != chat.video_id:
                    changes.setdefault("summary", None)
                    changes.setdefault("transcript", None)
                    changes.setdefault("video_metadata", None)
                if "title" in changes:
                    changes["title"] = (changes["title"] or "").strip() or "New chat"
                for key in ("video_id", "title", "video_metadata", "summary", "transcript"):
                    if key in changes:
                        setattr(chat, key, changes[key])
                if request.messages is not None:
                    self._replace_messages(session, chat, request.messages)
                chat.updated_at = now
                session.flush()
                session.refresh(chat)
                session.expire(chat, ["messages"])
                self._load_messages(session, chat)
                record = self._to_record(chat, username)
            return record
        except ChatNotFoundError:
            raise
        except IntegrityError as exc:
            raise ChatStoreError("Chat storage is unavailable") from exc
        except SQLAlchemyError as exc:
            raise ChatStoreError("Chat storage is unavailable") from exc

    def append_message(self, username: str, chat_id: str, message: ChatMessage) -> ChatRecord:
        self._validate_chat_id(chat_id)
        try:
            with self.session_factory.begin() as session:
                now = utc_now()
                values: dict[str, object] = {"updated_at": now}
                if message.role == "user":
                    values["title"] = case(
                        (Chat.title == "New chat", message.content.strip()[:80] or "New chat"),
                        else_=Chat.title,
                    )
                changed = session.execute(
                    update(Chat)
                    .where(
                        Chat.id == chat_id,
                        Chat.user_id == select(User.id).where(User.username == username.casefold()).scalar_subquery(),
                    )
                    .values(**values)
                )
                if changed.rowcount != 1:
                    raise ChatNotFoundError
                self._add_message(session, chat_id, message)
                chat = self._get_in_session(session, username, chat_id)
                session.flush()
                session.expire(chat, ["messages"])
                self._load_messages(session, chat)
                record = self._to_record(chat, username)
            return record
        except ChatNotFoundError:
            raise
        except IntegrityError as exc:
            raise ChatStoreError("Chat storage is unavailable") from exc
        except SQLAlchemyError as exc:
            raise ChatStoreError("Chat storage is unavailable") from exc

    def set_message_audio_ref(self, username: str, chat_id: str, message_id: str, audio_ref: str) -> ChatMessage | None:
        self._validate_chat_id(chat_id)
        try:
            with self.session_factory.begin() as session:
                message = session.scalar(
                    select(Message)
                    .join(Chat)
                    .join(User)
                    .where(
                        Chat.id == chat_id,
                        User.username == username.casefold(),
                        Message.id == message_id,
                        Message.role == "assistant",
                    )
                    .options(selectinload(Message.sources))
                )
                if message is None:
                    if self._get_in_session(session, username, chat_id) is None:
                        raise ChatNotFoundError
                    return None
                message.answer_audio_ref = audio_ref
                message.chat.updated_at = utc_now()
                session.flush()
                return self._to_message(message)
        except ChatNotFoundError:
            raise
        except SQLAlchemyError as exc:
            raise ChatStoreError("Chat storage is unavailable") from exc

    def delete(self, username: str, chat_id: str) -> None:
        try:
            with self.session_factory.begin() as session:
                chat = self._get_in_session(session, username, chat_id, lock=True)
                session.delete(chat)
        except ChatNotFoundError:
            raise
        except SQLAlchemyError as exc:
            raise ChatStoreError("Chat storage is unavailable") from exc

    def _fetch(self, username: str, chat_id: str) -> Chat:
        try:
            with self.session_factory() as session:
                chat = self._get_in_session(session, username, chat_id)
                session.expunge_all()
                return chat
        except ChatNotFoundError:
            raise
        except SQLAlchemyError as exc:
            raise ChatStoreError("Chat storage is unavailable") from exc

    def _get_in_session(self, session: OrmSession, username: str, chat_id: str, lock: bool = False) -> Chat:
        self._validate_chat_id(chat_id)
        statement = (
            select(Chat)
            .join(User)
            .where(Chat.id == chat_id, User.username == username.casefold())
            .options(selectinload(Chat.messages).selectinload(Message.sources))
        )
        if lock:
            statement = statement.with_for_update()
        chat = session.scalar(statement)
        if chat is None:
            raise ChatNotFoundError
        return chat

    @staticmethod
    def _validate_chat_id(chat_id: str) -> None:
        if not CHAT_ID_PATTERN.fullmatch(chat_id):
            raise InvalidChatIdError

    @staticmethod
    def _add_message(session: OrmSession, chat_id: str, message: ChatMessage) -> Message:
        stored = Message(
            id=message.id,
            chat_id=chat_id,
            role=message.role,
            content=message.content,
            created_at=message.timestamp,
            answer_audio_ref=message.answer_audio_ref,
            timestamps=message.timestamps,
        )
        session.add(stored)
        session.flush()
        for position, source in enumerate(message.sources):
            session.add(
                MessageSource(
                    message_id=message.id,
                    position=position,
                    text=source.text,
                    start_s=source.start,
                    end_s=source.end,
                )
            )
        return stored

    def _replace_messages(self, session: OrmSession, chat: Chat, messages: list[ChatMessage]) -> None:
        chat.messages.clear()
        session.flush()
        for message in messages:
            self._add_message(session, chat.id, message)
        self._load_messages(session, chat)

    @staticmethod
    def _load_messages(session: OrmSession, chat: Chat) -> None:
        chat.messages = session.scalars(
            select(Message)
            .where(Message.chat_id == chat.id)
            .options(selectinload(Message.sources))
            .order_by(Message.created_at, Message.id)
        ).all()

    @staticmethod
    def _to_message(message: Message) -> ChatMessage:
        timestamp = message.created_at
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return ChatMessage(
            id=message.id,
            role=message.role,
            content=message.content,
            timestamp=timestamp,
            sources=[
                {"start": source.start_s, "end": source.end_s, "text": source.text}
                for source in sorted(message.sources, key=lambda item: item.position)
            ],
            timestamps=message.timestamps or [],
            answer_audio_ref=message.answer_audio_ref,
        )

    def _to_record(self, chat: Chat, username: str) -> ChatRecord:
        created_at = chat.created_at
        updated_at = chat.updated_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        return ChatRecord(
            chat_id=chat.id,
            username=username.casefold(),
            title=chat.title,
            created_at=created_at,
            updated_at=updated_at,
            video_id=chat.video_id,
            video_metadata=chat.video_metadata,
            summary=chat.summary,
            transcript=chat.transcript,
            messages=[self._to_message(message) for message in chat.messages],
        )
