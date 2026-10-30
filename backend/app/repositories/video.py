import re
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session as OrmSession

from backend.app.db.models import User, Video


VIDEO_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class VideoRepositoryError(Exception):
    pass


class VideoRepo:
    def __init__(self, session_factory: Callable[[], OrmSession]) -> None:
        self.session_factory = session_factory

    def register(
        self,
        video_id: str,
        username: str,
        source_type: str = "upload",
        source_url: str | None = None,
        duration_s: float | None = None,
    ) -> None:
        if not VIDEO_ID_PATTERN.fullmatch(video_id):
            raise VideoRepositoryError("Video ownership is unavailable")
        try:
            with self.session_factory.begin() as session:
                user = session.scalar(select(User).where(User.username == username.casefold()))
                if user is None:
                    raise VideoRepositoryError("Video ownership is unavailable")
                video = session.get(Video, video_id)
                if video is not None:
                    if video.user_id != user.id:
                        raise VideoRepositoryError("Video ownership is unavailable")
                    video.source_type = source_type
                    video.source_url = source_url
                    video.duration_s = duration_s
                else:
                    session.add(
                        Video(
                            id=video_id,
                            user_id=user.id,
                            source_type=source_type,
                            source_url=source_url,
                            duration_s=duration_s,
                            status="queued",
                        )
                    )
        except VideoRepositoryError:
            raise
        except IntegrityError as exc:
            raise VideoRepositoryError("Video ownership is unavailable") from exc
        except SQLAlchemyError as exc:
            raise VideoRepositoryError("Video ownership is unavailable") from exc

    def check_quota(
        self,
        username: str,
        duration_s: float,
        max_videos: int,
        max_total_minutes: int,
    ) -> bool:
        try:
            with self.session_factory() as session:
                user_id = session.scalar(select(User.id).where(User.username == username.casefold()))
                if user_id is None:
                    return False
                count, duration_total = session.execute(
                    select(func.count(Video.id), func.coalesce(func.sum(Video.duration_s), 0.0))
                    .where(Video.user_id == user_id)
                ).one()
                return (
                    count < max_videos
                    and duration_total + duration_s <= max_total_minutes * 60
                )
        except SQLAlchemyError as exc:
            raise VideoRepositoryError("Video ownership is unavailable") from exc

    def is_owned_by(self, video_id: str, username: str) -> bool:
        if not VIDEO_ID_PATTERN.fullmatch(video_id):
            return False
        try:
            with self.session_factory() as session:
                return session.scalar(
                    select(Video.id)
                    .join(User)
                    .where(Video.id == video_id, User.username == username.casefold())
                ) is not None
        except SQLAlchemyError as exc:
            raise VideoRepositoryError("Video ownership is unavailable") from exc
