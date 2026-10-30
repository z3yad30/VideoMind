from pathlib import Path

from backend.app.db.database import get_database
from backend.app.repositories.video import VideoRepo, VideoRepositoryError


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
