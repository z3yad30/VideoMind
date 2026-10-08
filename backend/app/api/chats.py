import asyncio
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from backend.app.api.dependencies import get_current_user
from backend.app.api.videos import ai_service, video_service, voice_service
from backend.app.schemas.chats import (
    ChatCreate,
    ChatListItem,
    ChatMessage,
    ChatQuestionResponse,
    ChatRecord,
    ChatSource,
    ChatUpdate,
)
from backend.app.schemas.videos import QuestionRequest
from backend.app.services.auth import AuthenticatedUser
from backend.app.services.chat_storage import (
    ChatNotFoundError,
    ChatStoreError,
    InvalidChatIdError,
    chat_store,
)
from backend.app.services.video_storage import VideoOwnershipError, video_ownership


router = APIRouter(prefix="/chats", tags=["chats"])
_ANSWER_AUDIO_ID = re.compile(r"^[0-9a-f]{32}$")
_AUDIO_GENERATION_LOCKS: dict[tuple[str, str, str], asyncio.Lock] = {}


def _chat_audio_location(chat_id: str, message_id: str, answer_id: str) -> str:
    return f"/chats/{chat_id}/messages/{message_id}/audio/{answer_id}"


def _message_answer_id(chat_id: str, message_id: str, audio_ref: str | None) -> str | None:
    if not audio_ref:
        return None
    prefix = f"/chats/{chat_id}/messages/{message_id}/audio/"
    if audio_ref.startswith(prefix):
        answer_id = audio_ref[len(prefix):]
        return answer_id if _ANSWER_AUDIO_ID.fullmatch(answer_id) else None
    return None


def _answer_audio_path(video_id: str, answer_id: str) -> Path | None:
    if not re.fullmatch(r"[0-9a-f]{32}", video_id) or not _ANSWER_AUDIO_ID.fullmatch(answer_id):
        return None
    audio_root = voice_service.audio_dir
    video_audio_dir = audio_root / video_id
    audio_path = video_audio_dir / f"{answer_id}.wav"
    if audio_root.is_symlink() or video_audio_dir.is_symlink() or audio_path.is_symlink():
        return None
    try:
        audio_path.resolve().relative_to(audio_root.resolve())
    except (OSError, ValueError):
        return None
    return audio_path


def _owned_message(chat_id: str, message_id: str, username: str):
    try:
        chat = chat_store.get(username, chat_id)
    except (ChatNotFoundError, ChatStoreError, InvalidChatIdError) as exc:
        _raise_storage_error(exc)
    message = next((item for item in chat.messages if item.id == message_id and item.role == "assistant"), None)
    if message is None:
        raise HTTPException(status_code=404, detail="Answer audio not found")
    return chat, message


def _raise_storage_error(error: Exception) -> None:
    if isinstance(error, ChatNotFoundError):
        raise HTTPException(status_code=404, detail="Chat not found") from error
    if isinstance(error, InvalidChatIdError):
        raise HTTPException(status_code=422, detail="Invalid chat ID") from error
    raise HTTPException(status_code=503, detail="Chat storage is unavailable") from error


def _require_video_owner(video_id: str | None, username: str) -> None:
    if not video_id:
        return
    try:
        owned = video_ownership.is_owned_by(video_id, username)
    except VideoOwnershipError as exc:
        raise HTTPException(status_code=503, detail="Video storage is unavailable") from exc
    if not owned:
        raise HTTPException(status_code=404, detail="Video not found")


@router.get("", response_model=list[ChatListItem])
def list_chats(user: AuthenticatedUser = Depends(get_current_user)) -> list[ChatListItem]:
    try:
        return [ChatListItem.model_validate(chat.model_dump()) for chat in chat_store.list_for_user(user.username)]
    except ChatStoreError as exc:
        _raise_storage_error(exc)


@router.post("", response_model=ChatRecord, status_code=status.HTTP_201_CREATED)
def create_chat(request: ChatCreate, user: AuthenticatedUser = Depends(get_current_user)) -> ChatRecord:
    _require_video_owner(request.video_id, user.username)
    try:
        return chat_store.create(user.username, request)
    except ChatStoreError as exc:
        _raise_storage_error(exc)


@router.post("/{chat_id}/messages", response_model=ChatQuestionResponse)
async def ask_in_chat(
    chat_id: str,
    request: QuestionRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ChatQuestionResponse:
    try:
        chat = chat_store.get(user.username, chat_id)
    except (ChatNotFoundError, ChatStoreError, InvalidChatIdError) as exc:
        _raise_storage_error(exc)

    if not chat.video_id:
        raise HTTPException(status_code=400, detail="Chat is not associated with a video")
    job = video_service.get_job(chat.video_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Video not found")
    if job.status != "completed":
        raise HTTPException(status_code=409, detail="Video processing is not complete")

    question = request.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question must not be empty")
    user_message = ChatMessage(role="user", content=question)
    try:
        chat_store.append_message(user.username, chat_id, user_message)
    except (ChatNotFoundError, ChatStoreError, InvalidChatIdError) as exc:
        _raise_storage_error(exc)

    history = [
        {"role": message.role, "content": message.content}
        for message in chat.messages
        if message.role in {"user", "assistant"}
    ][-3:]
    try:
        result = await run_in_threadpool(
            ai_service.answer_question,
            chat.video_id,
            question,
            history=history,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    sources = [ChatSource.model_validate(source) for source in result["sources"]]
    assistant_message = ChatMessage(
        role="assistant",
        content=str(result["answer"]),
        sources=sources,
        timestamps=list(dict.fromkeys(time for source in sources for time in (source.start, source.end))),
    )
    audio_location = None
    try:
        answer_id, _ = await run_in_threadpool(voice_service.synthesize_answer, chat.video_id, str(result["answer"]))
        audio_location = _chat_audio_location(chat_id, assistant_message.id, answer_id)
        assistant_message = assistant_message.model_copy(update={"answer_audio_ref": audio_location})
    except RuntimeError:
        pass
    try:
        chat_store.append_message(user.username, chat_id, assistant_message)
    except (ChatNotFoundError, ChatStoreError, InvalidChatIdError) as exc:
        _raise_storage_error(exc)

    return ChatQuestionResponse(
        user_message=user_message,
        assistant_message=assistant_message,
        sources=sources,
        answer_audio_location=audio_location,
    )


@router.post("/{chat_id}/messages/{message_id}/audio")
async def generate_chat_answer_audio(
    chat_id: str,
    message_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, str]:
    chat, message = _owned_message(chat_id, message_id, user.username)
    lock_key = (user.username, chat_id, message_id)
    lock = _AUDIO_GENERATION_LOCKS.setdefault(lock_key, asyncio.Lock())
    async with lock:
        chat, message = _owned_message(chat_id, message_id, user.username)
        answer_id = _message_answer_id(chat_id, message_id, message.answer_audio_ref)
        if answer_id:
            audio_path = _answer_audio_path(str(chat.video_id), answer_id)
            if audio_path and audio_path.is_file():
                return {"answer_audio_ref": message.answer_audio_ref or ""}
        if not chat.video_id or not re.fullmatch(r"[0-9a-f]{32}", chat.video_id):
            raise HTTPException(status_code=404, detail="Answer audio not found")
        job = video_service.get_job(chat.video_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Video not found")
        if job.status != "completed":
            raise HTTPException(status_code=409, detail="Video processing is not complete")
        try:
            answer_id, audio_path = await run_in_threadpool(
                voice_service.synthesize_answer, chat.video_id, message.content
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        expected_path = _answer_audio_path(chat.video_id, answer_id)
        if expected_path is None or not audio_path.is_file() or audio_path.is_symlink():
            raise HTTPException(status_code=503, detail="TTS did not produce an audio file")
        try:
            if audio_path.resolve() != expected_path.resolve():
                raise HTTPException(status_code=503, detail="TTS produced an invalid audio location")
        except OSError as exc:
            raise HTTPException(status_code=503, detail="TTS did not produce an audio file") from exc
        audio_ref = _chat_audio_location(chat_id, message_id, answer_id)
        try:
            saved_message = chat_store.set_message_audio_ref(user.username, chat_id, message_id, audio_ref)
        except (ChatNotFoundError, ChatStoreError, InvalidChatIdError) as exc:
            _raise_storage_error(exc)
        if saved_message is None:
            raise HTTPException(status_code=404, detail="Answer audio not found")
        return {"answer_audio_ref": audio_ref}


@router.get("/{chat_id}/messages/{message_id}/audio")
def get_chat_answer_audio(
    chat_id: str,
    message_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> FileResponse:
    chat, message = _owned_message(chat_id, message_id, user.username)
    answer_id = _message_answer_id(chat_id, message_id, message.answer_audio_ref)
    if not answer_id or not chat.video_id or not re.fullmatch(r"[0-9a-f]{32}", chat.video_id):
        raise HTTPException(status_code=404, detail="Answer audio not found")
    job = video_service.get_job(chat.video_id)
    if job is None or job.status != "completed":
        raise HTTPException(status_code=404, detail="Answer audio not found")
    audio_path = _answer_audio_path(chat.video_id, answer_id)
    if audio_path is None or not audio_path.is_file():
        raise HTTPException(status_code=404, detail="Answer audio not found")
    return FileResponse(audio_path, media_type="audio/wav", filename=f"{answer_id}.wav")


@router.get("/{chat_id}", response_model=ChatRecord)
def get_chat(chat_id: str, user: AuthenticatedUser = Depends(get_current_user)) -> ChatRecord:
    try:
        chat = chat_store.get(user.username, chat_id)
        _require_video_owner(chat.video_id, user.username)
        return chat
    except (ChatNotFoundError, ChatStoreError, InvalidChatIdError) as exc:
        _raise_storage_error(exc)


@router.put("/{chat_id}", response_model=ChatRecord)
def update_chat(chat_id: str, request: ChatUpdate, user: AuthenticatedUser = Depends(get_current_user)) -> ChatRecord:
    try:
        existing = chat_store.get(user.username, chat_id)
        _require_video_owner(existing.video_id, user.username)
        changes = request.model_dump(exclude_unset=True)
        _require_video_owner(changes.get("video_id", existing.video_id), user.username)
        return chat_store.update(user.username, chat_id, request)
    except (ChatNotFoundError, ChatStoreError, InvalidChatIdError) as exc:
        _raise_storage_error(exc)


@router.delete("/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_chat(chat_id: str, user: AuthenticatedUser = Depends(get_current_user)) -> None:
    try:
        chat_store.delete(user.username, chat_id)
    except (ChatNotFoundError, ChatStoreError, InvalidChatIdError) as exc:
        _raise_storage_error(exc)