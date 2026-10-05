from fastapi import HTTPException, Request, status

from backend.app.services.auth import AuthenticatedUser, auth_service


SESSION_COOKIE_NAME = "videomind_session"


def get_current_user(request: Request) -> AuthenticatedUser:
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    user = auth_service.get_session_user(session_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Cookie"},
        )
    return user