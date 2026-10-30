from pathlib import Path

from backend.app.db.database import get_database
from backend.app.repositories.video import AssetCleanup, UploadRegistration, VideoRepo, VideoRepositoryError


class VideoOwnershipError(Exception):
    pass


class VideoOwnershipStore:
    def __init__(self, repository: VideoRepo | Path | None = None) -> None:
        if isinstance(repository, Path):
            raise TypeError("VideoOwnershipStore requires a VideoRepo, not a directory path")
        self.repository = repository or VideoRepo(get_database().session_factory)

    def register(
        self,
        video_id: str,
        username: str,
        source_type: str = "upload",
        source_url: str | None = None,
        duration_s: float | None = None,
    ) -> None:
        try:
            self.repository.register(video_id, username, source_type, source_url, duration_s)
        except VideoRepositoryError as exc:
            raise VideoOwnershipError("Video ownership is unavailable") from exc

    def register_upload(
        self,
        video_id: str,
        username: str,
        sha256: str,
        size: int,
        duration_s: float,
        suffix: str,
    ) -> UploadRegistration:
        try:
            return self.repository.register_upload(
                video_id, username, sha256, size, duration_s, suffix
            )
        except VideoRepositoryError as exc:
            raise VideoOwnershipError("Video ownership is unavailable") from exc

    def asset_id_for_video(self, video_id: str, username: str) -> str | None:
        try:
            return self.repository.asset_id_for_video(video_id, username)
        except VideoRepositoryError as exc:
            raise VideoOwnershipError("Video storage is unavailable") from exc

    def blob_key_for_video(self, video_id: str, username: str) -> str | None:
        try:
            return self.repository.blob_key_for_video(video_id, username)
        except VideoRepositoryError as exc:
            raise VideoOwnershipError("Video storage is unavailable") from exc

    def set_asset_status(self, asset_id: str, status: str) -> None:
        try:
            self.repository.set_asset_status(asset_id, status)
        except VideoRepositoryError as exc:
            raise VideoOwnershipError("Video storage is unavailable") from exc

    def delete_video(self, video_id: str, username: str) -> list[AssetCleanup]:
        try:
            return self.repository.delete_video(video_id, username)
        except VideoRepositoryError as exc:
            raise VideoOwnershipError("Unable to delete video") from exc

    def delete_user_data(self, username: str) -> list[AssetCleanup]:
        try:
            return self.repository.delete_user_data(username)
        except VideoRepositoryError as exc:
            raise VideoOwnershipError("Unable to delete user data") from exc

    def check_quota(
        self,
        username: str,
        duration_s: float,
        max_videos: int,
        max_total_minutes: int,
    ) -> bool:
        try:
            return self.repository.check_quota(username, duration_s, max_videos, max_total_minutes)
        except VideoRepositoryError as exc:
            raise VideoOwnershipError("Video ownership is unavailable") from exc

    def is_owned_by(self, video_id: str, username: str) -> bool:
        try:
            return self.repository.is_owned_by(video_id, username)
        except VideoRepositoryError as exc:
            raise VideoOwnershipError("Video ownership is unavailable") from exc


video_ownership = VideoOwnershipStore()


__all__ = ["VideoOwnershipError", "VideoOwnershipStore", "video_ownership"]
