from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from backend.app.api.dependencies import SESSION_COOKIE_NAME, get_current_user
from backend.app.schemas.auth import CredentialsRequest, UserResponse
from backend.app.services.auth import (
    AuthenticatedUser,
    DuplicateUserError,
    SESSION_TTL_SECONDS,
    auth_service,
)
from backend.app.services.user_storage import UserStoreError


router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(request: CredentialsRequest) -> UserResponse:
    try:
        user = auth_service.register(request.username, request.password)
    except DuplicateUserError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username is already registered") from exc
    except UserStoreError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="User storage is unavailable") from exc
    return UserResponse(username=user.username)


@router.post("/login", response_model=UserResponse)
def login(request: CredentialsRequest, response: Response) -> UserResponse:
    try:
        user = auth_service.authenticate(request.username, request.password)
    except UserStoreError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="User storage is unavailable") from exc
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")
    session_id = auth_service.create_session(user)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )
    return UserResponse(username=user.username)


@router.get("/me", response_model=UserResponse)
def get_me(user: AuthenticatedUser = Depends(get_current_user)) -> UserResponse:
    return UserResponse(username=user.username)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request) -> Response:
    auth_service.invalidate_session(request.cookies.get(SESSION_COOKIE_NAME))
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return response