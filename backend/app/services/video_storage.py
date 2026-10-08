import json
import os
from pathlib import Path
import re
import tempfile
from threading import RLock

from backend.app.core.config import settings


VIDEO_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class VideoOwnershipError(Exception):
    pass


class VideoOwnershipStore:
    def __init__(self, video_dir: Path | None = None) -> None:
        self.video_dir = video_dir or settings.project_root / "data" / "videos"
        self.path = self.video_dir / ".owners.json"
        self._lock = RLock()

    def register(self, video_id: str, username: str) -> None:
        if not VIDEO_ID_PATTERN.fullmatch(video_id):
            raise VideoOwnershipError("Video ownership is unavailable")
        with self._lock:
            owners = self._read()
            owner = owners.get(video_id)
            if owner is not None and owner != username:
                raise VideoOwnershipError("Video ownership is unavailable")
            owners[video_id] = username
            self._write(owners)

    def is_owned_by(self, video_id: str, username: str) -> bool:
        if not VIDEO_ID_PATTERN.fullmatch(video_id):
            return False
        with self._lock:
            return self._read().get(video_id) == username

    def _read(self) -> dict[str, str]:
        if self.video_dir.is_symlink() or self.path.is_symlink():
            raise VideoOwnershipError("Video ownership is unavailable")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise VideoOwnershipError("Video ownership is unavailable") from exc
        if not isinstance(payload, dict) or any(
            not VIDEO_ID_PATTERN.fullmatch(video_id)
            or not isinstance(username, str)
            for video_id, username in payload.items()
        ):
            raise VideoOwnershipError("Video ownership is unavailable")
        return payload

    def _write(self, owners: dict[str, str]) -> None:
        temporary_name: str | None = None
        try:
            if self.video_dir.is_symlink() or self.path.is_symlink():
                raise VideoOwnershipError("Video ownership is unavailable")
            self.video_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.video_dir,
                prefix=".owners.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_name = temporary_file.name
                json.dump(owners, temporary_file, ensure_ascii=True, sort_keys=True)
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_name, self.path)
        except VideoOwnershipError:
            raise
        except OSError as exc:
            raise VideoOwnershipError("Video ownership is unavailable") from exc
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)


video_ownership = VideoOwnershipStore()


__all__ = ["VideoOwnershipError", "VideoOwnershipStore", "video_ownership"]