import re
from dataclasses import dataclass
from typing import Callable
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session as OrmSession

from backend.app.db.models import Chat, Job, MediaAsset, Session, User, Video


VIDEO_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class VideoRepositoryError(Exception):
    pass


@dataclass(frozen=True)
class UploadRegistration:
    asset_id: str
    blob_key: str
    duplicate: bool
    completed: bool
    enqueue: bool


@dataclass(frozen=True)
class AssetCleanup:
    asset_id: str
    blob_key: str


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

    def register_upload(
        self,
        video_id: str,
        username: str,
        sha256: str,
        size: int,
        duration_s: float,
        suffix: str,
    ) -> UploadRegistration:
        if not VIDEO_ID_PATTERN.fullmatch(video_id):
            raise VideoRepositoryError("Video ownership is unavailable")
        for attempt in range(2):
            try:
                with self.session_factory.begin() as session:
                    user = session.scalar(select(User).where(User.username == username.casefold()))
                    if user is None:
                        raise VideoRepositoryError("Video ownership is unavailable")
                    asset = session.scalar(select(MediaAsset).where(MediaAsset.sha256 == sha256))
                    duplicate = asset is not None
                    enqueue = True
                    if asset is None:
                        asset_id = uuid4().hex
                        asset = MediaAsset(
                            id=asset_id,
                            sha256=sha256,
                            size=size,
                            duration_s=duration_s,
                            blob_key=f"videos/assets/{asset_id}{suffix}",
                            status="queued",
                        )
                        session.add(asset)
                        session.flush()
                    elif asset.status == "completed":
                        enqueue = False
                    else:
                        active_job_id = session.scalar(
                            select(Job.id)
                            .join(Video, Job.video_id == Video.id)
                            .where(
                                Video.asset_id == asset.id,
                                Job.state.in_(("queued", "running")),
                            )
                            .limit(1)
                        )
                        enqueue = active_job_id is None
                    session.add(
                        Video(
                            id=video_id,
                            user_id=user.id,
                            asset_id=asset.id,
                            source_type="upload",
                            duration_s=duration_s,
                            status="completed" if asset.status == "completed" else "queued",
                        )
                    )
                    return UploadRegistration(
                        asset.id,
                        asset.blob_key,
                        duplicate,
                        asset.status == "completed",
                        enqueue,
                    )
            except IntegrityError as exc:
                if attempt == 0:
                    continue
                raise VideoRepositoryError("Unable to register uploaded media") from exc
            except VideoRepositoryError:
                raise
            except SQLAlchemyError as exc:
                raise VideoRepositoryError("Unable to register uploaded media") from exc
        raise VideoRepositoryError("Unable to register uploaded media")

    def asset_id_for_video(self, video_id: str, username: str) -> str | None:
        try:
            with self.session_factory() as session:
                return session.scalar(
                    select(Video.asset_id)
                    .join(User)
                    .where(Video.id == video_id, User.username == username.casefold())
                )
        except SQLAlchemyError as exc:
            raise VideoRepositoryError("Video storage is unavailable") from exc

    def blob_key_for_video(self, video_id: str, username: str) -> str | None:
        try:
            with self.session_factory() as session:
                return session.scalar(
                    select(MediaAsset.blob_key)
                    .join(Video, Video.asset_id == MediaAsset.id)
                    .join(User, Video.user_id == User.id)
                    .where(Video.id == video_id, User.username == username.casefold())
                )
        except SQLAlchemyError as exc:
            raise VideoRepositoryError("Video storage is unavailable") from exc

    def set_asset_status(self, asset_id: str, status: str) -> None:
        try:
            with self.session_factory.begin() as session:
                session.execute(update(MediaAsset).where(MediaAsset.id == asset_id).values(status=status))
                session.execute(update(Video).where(Video.asset_id == asset_id).values(status=status))
        except SQLAlchemyError as exc:
            raise VideoRepositoryError("Video storage is unavailable") from exc

    def delete_video(self, video_id: str, username: str) -> list[AssetCleanup]:
        try:
            with self.session_factory.begin() as session:
                video = session.scalar(
                    select(Video)
                    .join(User)
                    .where(Video.id == video_id, User.username == username.casefold())
                    .with_for_update()
                )
                if video is None:
                    return []
                asset_id = video.asset_id
                if asset_id is not None:
                    replacement_id = session.scalar(
                        select(Video.id)
                        .where(Video.asset_id == asset_id, Video.id != video_id)
                        .order_by(Video.created_at, Video.id)
                    )
                    job = session.scalar(select(Job).where(Job.video_id == video_id))
                    if replacement_id is not None and job is not None:
                        replacement_has_job = session.scalar(
                            select(Job.id).where(Job.video_id == replacement_id)
                        )
                        if replacement_has_job is None:
                            job.video_id = replacement_id
                session.execute(
                    update(Chat)
                    .where(Chat.video_id == video.id)
                    .values(video_id=None, video_missing=True)
                )
                session.delete(video)
                if asset_id is None:
                    return [AssetCleanup(video.id, f"videos/{video.id}")]
                remaining = session.scalar(select(func.count(Video.id)).where(Video.asset_id == asset_id))
                if remaining:
                    return []
                asset = session.get(MediaAsset, asset_id)
                if asset is None:
                    return []
                cleanup = AssetCleanup(asset.id, asset.blob_key)
                session.delete(asset)
                return [cleanup]
        except SQLAlchemyError as exc:
            raise VideoRepositoryError("Unable to delete video") from exc

    def delete_user_data(self, username: str) -> list[AssetCleanup]:
        try:
            with self.session_factory.begin() as session:
                user = session.scalar(select(User).where(User.username == username.casefold()))
                if user is None:
                    raise VideoRepositoryError("Video storage is unavailable")
                video_ids = list(session.scalars(select(Video.id).where(Video.user_id == user.id)).all())
                unlinked_video_ids = set(
                    session.scalars(
                        select(Video.id).where(Video.user_id == user.id, Video.asset_id.is_(None))
                    ).all()
                )
                asset_ids = set(
                    item
                    for item in session.scalars(
                        select(Video.asset_id).where(Video.user_id == user.id, Video.asset_id.is_not(None))
                    ).all()
                    if item is not None
                )
                for asset_id in asset_ids:
                    replacement_ids = list(
                        session.scalars(
                            select(Video.id)
                            .where(Video.asset_id == asset_id, Video.user_id != user.id)
                            .order_by(Video.created_at, Video.id)
                        ).all()
                    )
                    for video_id in video_ids:
                        video = session.get(Video, video_id)
                        if video is None or video.asset_id != asset_id:
                            continue
                        job = session.scalar(select(Job).where(Job.video_id == video_id))
                        if job is None:
                            continue
                        replacement_id = next(
                            (
                                candidate_id
                                for candidate_id in replacement_ids
                                if session.scalar(select(Job.id).where(Job.video_id == candidate_id)) is None
                            ),
                            None,
                        )
                        if replacement_id is not None:
                            job.video_id = replacement_id
                if video_ids:
                    session.execute(
                        update(Chat)
                        .where(Chat.video_id.in_(video_ids))
                        .values(video_id=None, video_missing=True)
                    )
                session.execute(delete(Chat).where(Chat.user_id == user.id))
                session.execute(delete(Session).where(Session.user_id == user.id))
                if video_ids:
                    session.execute(delete(Job).where(Job.video_id.in_(video_ids)))
                    session.execute(delete(Video).where(Video.id.in_(video_ids)))

                cleanup: list[AssetCleanup] = [
                    AssetCleanup(video_id, f"videos/{video_id}") for video_id in unlinked_video_ids
                ]
                for asset_id in asset_ids:
                    remaining = session.scalar(select(func.count(Video.id)).where(Video.asset_id == asset_id))
                    if remaining:
                        continue
                    asset = session.get(MediaAsset, asset_id)
                    if asset is not None:
                        cleanup.append(AssetCleanup(asset.id, asset.blob_key))
                        session.delete(asset)
                return cleanup
        except VideoRepositoryError:
            raise
        except SQLAlchemyError as exc:
            raise VideoRepositoryError("Unable to delete user data") from exc

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
