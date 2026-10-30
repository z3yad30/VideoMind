from functools import lru_cache
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session as OrmSession, sessionmaker

from backend.app.core.config import settings
from backend.app.db.models import Base


def _sqlite_path(database_url: str) -> Path | None:
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite") or not url.database or url.database == ":memory:":
        return None
    value = url.database
    if value.startswith("file:"):
        return None
    path = Path(value)
    if not path.is_absolute():
        path = settings.project_root / path
    return path


def build_engine(database_url: str, busy_timeout_ms: int | None = None) -> Engine:
    sqlite_path = _sqlite_path(database_url)
    if sqlite_path is not None:
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(database_url, pool_pre_ping=True)
    if engine.dialect.name == "sqlite":
        timeout = settings.sqlite_busy_timeout_ms if busy_timeout_ms is None else busy_timeout_ms

        @event.listens_for(engine, "connect")
        def configure_sqlite(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute(f"PRAGMA busy_timeout={int(timeout)}")
                cursor.execute("PRAGMA journal_mode=WAL")
            finally:
                cursor.close()
    return engine


class Database:
    def __init__(self, database_url: str, busy_timeout_ms: int | None = None) -> None:
        self.engine = build_engine(database_url, busy_timeout_ms)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False, class_=OrmSession)

    def session(self) -> Iterator[OrmSession]:
        with self.session_factory() as session:
            yield session

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    def dispose(self) -> None:
        self.engine.dispose()


@lru_cache(maxsize=1)
def get_database() -> Database:
    return Database(settings.database_url, settings.sqlite_busy_timeout_ms)


def create_schema(database_url: str | None = None) -> None:
    database = get_database() if database_url is None else Database(database_url)
    try:
        database.create_schema()
    finally:
        if database_url is not None:
            database.dispose()
