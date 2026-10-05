from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool

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


router = APIRouter(prefix="/chats", tags=["chats"])


def _raise_storage_error(error: Exception) -> None:
    if isinstance(error, ChatNotFoundError):
        raise HTTPException(status_code=404, detail="Chat not found") from error
    if isinstance(error, InvalidChatIdError):
        raise HTTPException(status_code=422, detail="Invalid chat ID") from error
    raise HTTPException(status_code=503, detail="Chat storage is unavailable") from error


@router.get("", response_model=list[ChatListItem])
def list_chats(user: AuthenticatedUser = Depends(get_current_user)) -> list[ChatListItem]:
    try:
        return [ChatListItem.model_validate(chat.model_dump()) for chat in chat_store.list_for_user(user.username)]
    except ChatStoreError as exc:
        _raise_storage_error(exc)


@router.post("", response_model=ChatRecord, status_code=status.HTTP_201_CREATED)
def create_chat(request: ChatCreate, user: AuthenticatedUser = Depends(get_current_user)) -> ChatRecord:
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
    audio_location = None
    try:
        answer_id, _ = await run_in_threadpool(voice_service.synthesize_answer, chat.video_id, str(result["answer"]))
        audio_location = f"/videos/{chat.video_id}/answers/{answer_id}/audio"
    except RuntimeError:
        pass

    assistant_message = ChatMessage(
        role="assistant",
        content=str(result["answer"]),
        sources=sources,
        timestamps=list(dict.fromkeys(time for source in sources for time in (source.start, source.end))),
        answer_audio_ref=audio_location,
    )
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


@router.get("/{chat_id}", response_model=ChatRecord)
def get_chat(chat_id: str, user: AuthenticatedUser = Depends(get_current_user)) -> ChatRecord:
    try:
        return chat_store.get(user.username, chat_id)
    except (ChatNotFoundError, ChatStoreError, InvalidChatIdError) as exc:
        _raise_storage_error(exc)


@router.put("/{chat_id}", response_model=ChatRecord)
def update_chat(chat_id: str, request: ChatUpdate, user: AuthenticatedUser = Depends(get_current_user)) -> ChatRecord:
    try:
        return chat_store.update(user.username, chat_id, request)
    except (ChatNotFoundError, ChatStoreError, InvalidChatIdError) as exc:
        _raise_storage_error(exc)


@router.delete("/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_chat(chat_id: str, user: AuthenticatedUser = Depends(get_current_user)) -> None:
    try:
        chat_store.delete(user.username, chat_id)
    except (ChatNotFoundError, ChatStoreError, InvalidChatIdError) as exc:
        _raise_storage_error(exc)