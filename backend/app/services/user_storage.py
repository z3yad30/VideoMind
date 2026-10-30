from pathlib import Path
from datetime import datetime

from backend.app.db.database import get_database
from backend.app.repositories.user import DuplicateUserError, UserRepo, UserRepositoryError


class UserStoreError(Exception):
    pass


class UserStore:
    def __init__(self, repository: UserRepo | Path | None = None) -> None:
        if isinstance(repository, Path):
            raise TypeError("UserStore requires a UserRepo, not a JSON file path")
        self.repository = repository or UserRepo(get_database().session_factory)

    def find(self, username: str) -> dict[str, str] | None:
        try:
            return self.repository.find(username)
        except UserRepositoryError as exc:
            raise UserStoreError("User storage is unavailable") from exc

    def create(self, username: str, password_hash: str) -> None:
        try:
            self.repository.create(username, password_hash)
        except DuplicateUserError:
            raise
        except UserRepositoryError as exc:
            raise UserStoreError("User storage is unavailable") from exc

    def create_session(self, session_id: str, username: str, expires_at: datetime) -> None:
        try:
            user = self.repository.find(username)
            if user is None:
                raise UserStoreError("User storage is unavailable")
            self.repository.create_session(session_id, user["id"], expires_at)
        except UserRepositoryError as exc:
            raise UserStoreError("User storage is unavailable") from exc

    def get_session_user(self, session_id: str, now: datetime | None = None) -> str | None:
        try:
            return self.repository.get_session_user(session_id, now)
        except UserRepositoryError as exc:
            raise UserStoreError("User storage is unavailable") from exc

    def delete_session(self, session_id: str) -> None:
        try:
            self.repository.delete_session(session_id)
        except UserRepositoryError as exc:
            raise UserStoreError("User storage is unavailable") from exc


__all__ = ["DuplicateUserError", "UserStore", "UserStoreError"]
