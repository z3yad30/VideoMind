import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text

import backend.app.core.config as config_module
from backend.app.db.database import Database
from backend.app.db.models import Chat, Message, User, Video
from backend.app.schemas.chats import ChatMessage, ChatRecord
from scripts.migrate_json_to_db import import_json_data


def sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def test_sqlite_connection_pragmas_are_enabled(tmp_path: Path) -> None:
    database = Database(sqlite_url(tmp_path / "pragmas.db"), busy_timeout_ms=4321)
    with database.engine.connect() as connection:
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1
        assert connection.scalar(text("PRAGMA busy_timeout")) == 4321
        assert connection.scalar(text("PRAGMA journal_mode")).lower() == "wal"
    database.dispose()


def test_alembic_upgrade_creates_schema_on_temporary_database(
    tmp_path: Path, monkeypatch
) -> None:
    database_path = tmp_path / "migration.db"
    monkeypatch.setattr(
        config_module,
        "settings",
        replace(config_module.settings, database_url=sqlite_url(database_path)),
    )
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "backend" / "migrations"))
    command.upgrade(config, "head")

    database = Database(sqlite_url(database_path))
    try:
        assert {
            "users",
            "sessions",
            "media_assets",
            "videos",
            "chats",
            "messages",
            "message_sources",
            "jobs",
            "job_events",
            "alembic_version",
        }.issubset(inspect(database.engine).get_table_names())
        assert "duration_s" in {
            column["name"] for column in inspect(database.engine).get_columns("videos")
        }
        assert "status" in {
            column["name"] for column in inspect(database.engine).get_columns("media_assets")
        }
        assert "video_missing" in {
            column["name"] for column in inspect(database.engine).get_columns("chats")
        }
    finally:
        database.dispose()


def test_json_import_dry_run_and_repeated_import_are_idempotent(tmp_path: Path) -> None:
    data_dir = tmp_path / "legacy-data"
    (data_dir / "users").mkdir(parents=True)
    (data_dir / "videos").mkdir()
    (data_dir / "chats" / "alice").mkdir(parents=True)
    (data_dir / "users" / "users.json").write_text(
        json.dumps({"users": [{"username": "Alice", "password_hash": "argon2-hash"}]}),
        encoding="utf-8",
    )
    video_id = "a" * 32
    (data_dir / "videos" / ".owners.json").write_text(
        json.dumps({video_id: "alice"}), encoding="utf-8"
    )
    now = datetime.now(timezone.utc)
    chat_id = "b" * 32
    record = ChatRecord(
        chat_id=chat_id,
        username="alice",
        title="Legacy chat",
        created_at=now,
        updated_at=now,
        video_id=video_id,
        messages=[
            ChatMessage(
                id="assistant-message",
                role="assistant",
                content="Answer",
                sources=[{"text": "Evidence", "start": 1.0, "end": 2.0}],
            )
        ],
    )
    (data_dir / "chats" / "alice" / f"{chat_id}.json").write_text(
        record.model_dump_json(), encoding="utf-8"
    )

    database = Database(sqlite_url(tmp_path / "import.db"))
    database.create_schema()
    assert import_json_data(data_dir, database, dry_run=True) == {
        "users": 1,
        "videos": 1,
        "chats": 1,
        "messages": 1,
        "message_sources": 1,
    }
    with database.session_factory() as session:
        assert session.scalar(select(User.id)) is None

    expected = {"users": 1, "videos": 1, "chats": 1, "messages": 1, "message_sources": 1}
    assert import_json_data(data_dir, database) == expected
    assert import_json_data(data_dir, database) == {
        "users": 0,
        "videos": 0,
        "chats": 0,
        "messages": 0,
        "message_sources": 0,
    }
    with database.session_factory() as session:
        assert session.scalar(select(User.username)) == "alice"
        assert session.scalar(select(Video.id)) == video_id
        assert session.scalar(select(Chat.id)) == chat_id
        assert session.scalar(select(Message.id)) == "assistant-message"
    database.dispose()
