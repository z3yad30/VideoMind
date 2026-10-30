import secrets
from datetime import datetime, timedelta, timezone

from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError

from backend.app.services.user_storage import DuplicateUserError, UserStore, UserStoreError


SESSION_TTL_SECONDS = 12 * 60 * 60


class AuthenticatedUser:
    def __init__(self, username: str) -> None:
        self.username = username


class AuthenticationService:
    def __init__(self, user_store: UserStore) -> None:
        self.user_store = user_store
        self._password_hasher = PasswordHash.recommended()
        self._dummy_hash = self._password_hasher.hash(secrets.token_urlsafe(32))

    def register(self, username: str, password: str) -> AuthenticatedUser:
        password_hash = self._password_hasher.hash(password)
        self.user_store.create(username, password_hash)
        return AuthenticatedUser(username)

    def authenticate(self, username: str, password: str) -> AuthenticatedUser | None:
        record = self.user_store.find(username)
        password_hash = record["password_hash"] if record else self._dummy_hash
        try:
            verified = self._password_hasher.verify(password, password_hash)
        except UnknownHashError as exc:
            raise UserStoreError("User storage is unavailable") from exc
        except (TypeError, ValueError):
            verified = False
        if record is None or not verified:
            return None
        return AuthenticatedUser(record["username"])

    def create_session(self, user: AuthenticatedUser) -> str:
        session_id = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=SESSION_TTL_SECONDS)
        self.user_store.create_session(session_id, user.username, expires_at)
        return session_id

    def get_session_user(self, session_id: str | None) -> AuthenticatedUser | None:
        if not session_id:
            return None
        username = self.user_store.get_session_user(session_id, datetime.now(timezone.utc))
        return AuthenticatedUser(username) if username else None

    def invalidate_session(self, session_id: str | None) -> None:
        if session_id:
            self.user_store.delete_session(session_id)


auth_service = AuthenticationService(UserStore())


__all__ = ["AuthenticatedUser", "AuthenticationService", "DuplicateUserError", "auth_service"]