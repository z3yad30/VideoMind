from fastapi import APIRouter, Depends, HTTPException, status

from backend.app.api.dependencies import get_current_user
from backend.app.schemas.chats import ChatCreate, ChatListItem, ChatRecord, ChatUpdate
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