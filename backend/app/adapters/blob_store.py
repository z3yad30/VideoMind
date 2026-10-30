import os
import shutil
import tempfile
from pathlib import Path

from backend.app.ports import BlobStore


class LocalBlobStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    def open(self, key: str) -> Path:
        """Resolve a safe artifact key to its local path for streaming-friendly consumers."""
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def put(self, key: str, source: Path | bytes | str) -> Path:
        destination = self.open(key)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f"{destination.name}-", suffix=".tmp", dir=destination.parent)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as target:
                if isinstance(source, Path):
                    with source.open("rb") as origin:
                        while chunk := origin.read(1024 * 1024):
                            target.write(chunk)
                elif isinstance(source, bytes):
                    target.write(source)
                else:
                    target.write(source.encode("utf-8"))
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary_path, destination)
        finally:
            temporary_path.unlink(missing_ok=True)
        return destination

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def delete_prefix(self, prefix: str) -> None:
        path = self._path(prefix)
        if path.is_dir() and not path.is_symlink():
            for child in path.iterdir():
                if child.is_symlink() or child.is_file():
                    child.unlink()
                elif child.is_dir():
                    shutil.rmtree(child)
            path.rmdir()
        elif path.is_file() or path.is_symlink():
            path.unlink()

    def _path(self, key: str) -> Path:
        relative = Path(key)
        if not relative.parts or relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("Blob key must be a relative path")
        root = self.data_dir.resolve()
        path = root / relative
        try:
            path.resolve().relative_to(root)
        except ValueError as exc:
            raise ValueError("Blob key must remain within the data directory") from exc
        return path
