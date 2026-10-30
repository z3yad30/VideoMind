import hashlib
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session as OrmSession

from backend.app.db.models import Session, User


class UserRepositoryError(Exception):
    pass


class DuplicateUserError(Exception):
    pass


class UserRepo:
    def __init__(self, session_factory: Callable[[], OrmSession]) -> None:
        self.session_factory = session_factory

    def find(self, username: str) -> dict[str, str] | None:
        try:
            with self.session_factory() as session:
                user = session.scalar(select(User).where(User.username == username.casefold()))
                return {"id": user.id, "username": user.username, "password_hash": user.password_hash} if user else None
        except SQLAlchemyError as exc:
            raise UserRepositoryError from exc

    def create(self, username: str, password_hash: str) -> dict[str, str]:
        user = User(username=username.casefold(), password_hash=password_hash)
        try:
            with self.session_factory.begin() as session:
                session.add(user)
                session.flush()
            return {"id": user.id, "username": user.username, "password_hash": user.password_hash}
        except IntegrityError as exc:
            raise DuplicateUserError from exc
        except SQLAlchemyError as exc:
            raise UserRepositoryError from exc

    def get_by_id(self, user_id: str) -> dict[str, str] | None:
        try:
            with self.session_factory() as session:
                user = session.get(User, user_id)
                return {"id": user.id, "username": user.username, "password_hash": user.password_hash} if user else None
        except SQLAlchemyError as exc:
            raise UserRepositoryError from exc

    def create_session(self, session_id: str, user_id: str, expires_at: datetime) -> None:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        try:
            with self.session_factory.begin() as session:
                session.add(Session(id_hash=digest, user_id=user_id, expires_at=expires_at))
        except SQLAlchemyError as exc:
            raise UserRepositoryError from exc

    def get_session_user(self, session_id: str, now: datetime | None = None) -> str | None:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        current_time = now or datetime.now(timezone.utc)
        try:
            with self.session_factory.begin() as session:
                record = session.get(Session, digest)
                if record is None:
                    return None
                expires_at = record.expires_at
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if expires_at <= current_time:
                    session.delete(record)
                    return None
                user = session.get(User, record.user_id)
                return user.username if user else None
        except SQLAlchemyError as exc:
            raise UserRepositoryError from exc

    def delete_session(self, session_id: str) -> None:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        try:
            with self.session_factory.begin() as session:
                record = session.get(Session, digest)
                if record:
                    session.delete(record)
        except SQLAlchemyError as exc:
            raise UserRepositoryError from exc
