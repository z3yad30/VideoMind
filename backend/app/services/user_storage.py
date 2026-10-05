import json
import os
from pathlib import Path
import tempfile
from threading import RLock


class UserStoreError(Exception):
    pass


class DuplicateUserError(Exception):
    pass


class UserStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = RLock()

    def find(self, username: str) -> dict[str, str] | None:
        with self._lock:
            return self._read_users().get(username.casefold())

    def create(self, username: str, password_hash: str) -> None:
        normalized = username.casefold()
        with self._lock:
            users = self._read_users()
            if normalized in users:
                raise DuplicateUserError
            users[normalized] = {"username": normalized, "password_hash": password_hash}
            self._write_users(users)

    def _read_users(self) -> dict[str, dict[str, str]]:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                return {}
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            records = payload["users"]
            if not isinstance(records, list):
                raise ValueError("Invalid users list")
            users: dict[str, dict[str, str]] = {}
            for record in records:
                if not isinstance(record, dict):
                    raise ValueError("Invalid user record")
                username = record["username"]
                password_hash = record["password_hash"]
                if not isinstance(username, str) or not isinstance(password_hash, str) or not password_hash:
                    raise ValueError("Invalid user fields")
                key = username.casefold()
                if key in users:
                    raise ValueError("Duplicate username")
                users[key] = {"username": key, "password_hash": password_hash}
            return users
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise UserStoreError("User storage is unavailable") from exc

    def _write_users(self, users: dict[str, dict[str, str]]) -> None:
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = temporary_file.name
                json.dump({"users": list(users.values())}, temporary_file, ensure_ascii=True, indent=2)
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, self.path)
        except OSError as exc:
            raise UserStoreError("User storage is unavailable") from exc
        finally:
            if temporary_path is not None:
                Path(temporary_path).unlink(missing_ok=True)