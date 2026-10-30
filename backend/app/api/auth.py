from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from backend.app.api.dependencies import SESSION_COOKIE_NAME, get_current_user
from backend.app.core.config import settings
from backend.app.core.rate_limit import rate_limiter
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
def register(credentials: CredentialsRequest, request: Request) -> UserResponse:
    _check_auth_rate_limit(request, credentials.username, settings.register_rate_limit)
    try:
        user = auth_service.register(credentials.username, credentials.password)
    except DuplicateUserError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username is already registered") from exc
    except UserStoreError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="User storage is unavailable") from exc
    return UserResponse(username=user.username)


@router.post("/login", response_model=UserResponse)
def login(credentials: CredentialsRequest, request: Request, response: Response) -> UserResponse:
    _check_auth_rate_limit(request, credentials.username, settings.login_rate_limit)
    try:
        user = auth_service.authenticate(credentials.username, credentials.password)
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
        secure=settings.cookie_secure,
        path="/",
    )
    return UserResponse(username=user.username)


def _check_auth_rate_limit(request: Request, username: str, limit: str) -> None:
    client_ip = request.client.host if request.client else "unknown"
    key = f"{request.url.path}:{client_ip}:{username.strip().casefold()}"
    if not rate_limiter.hit(limit, key):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")


@router.get("/me", response_model=UserResponse)
def get_me(user: AuthenticatedUser = Depends(get_current_user)) -> UserResponse:
    return UserResponse(username=user.username)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request) -> Response:
    auth_service.invalidate_session(request.cookies.get(SESSION_COOKIE_NAME))
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response