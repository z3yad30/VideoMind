from pathlib import Path

from backend.app.db.database import Database
from backend.app.repositories.user import UserRepo


def create_database(tmp_path: Path) -> Database:
    db_path = tmp_path / "videomind.db"
    database = Database(f"sqlite:///{db_path.as_posix()}", busy_timeout_ms=10000)
    database.create_schema()
    return database


def seed_user(database: Database, username: str) -> None:
    UserRepo(database.session_factory).create(username, "test-password-hash")
