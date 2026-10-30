from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from backend.app.core.config import settings
from backend.app.db.database import Database
from backend.app.db.models import Chat, Message, MessageSource, User, Video
from backend.app.repositories.chat import CHAT_ID_PATTERN
from backend.app.schemas.chats import ChatRecord


class ImportError(RuntimeError):
    pass


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ImportError(f"Unable to read valid JSON from {path}") from exc


def import_json_data(data_dir: Path, database: Database, dry_run: bool = False) -> dict[str, int]:
    counts = {"users": 0, "videos": 0, "chats": 0, "messages": 0, "message_sources": 0}
    users_path = data_dir / "users" / "users.json"
    chats_root = data_dir / "chats"
    owners_path = data_dir / "videos" / ".owners.json"

    users_payload = _read_json(users_path) if users_path.exists() else {"users": []}
    if not isinstance(users_payload, dict) or not isinstance(users_payload.get("users", []), list):
        raise ImportError(f"Invalid users document: {users_path}")
    chat_files = sorted(chats_root.glob("**/*.json")) if chats_root.exists() else []
    owners_payload = _read_json(owners_path) if owners_path.exists() else {}
    if not isinstance(owners_payload, dict):
        raise ImportError(f"Invalid video owner document: {owners_path}")

    try:
        with database.session_factory() as session:
            transaction = session.begin()
            try:
                user_ids: dict[str, str] = {}
                for entry in users_payload.get("users", []):
                    if not isinstance(entry, dict):
                        raise ImportError("Invalid user record in legacy user store")
                    username = entry.get("username")
                    password_hash = entry.get("password_hash")
                    if not isinstance(username, str) or not isinstance(password_hash, str) or not password_hash:
                        raise ImportError("Legacy user record is missing username or password_hash")
                    username = username.casefold()
                    user = session.scalar(select(User).where(User.username == username))
                    if user is None:
                        user = User(username=username, password_hash=password_hash)
                        session.add(user)
                        session.flush()
                        counts["users"] += 1
                    user_ids[username] = user.id

                for video_id, username in sorted(owners_payload.items()):
                    if not isinstance(video_id, str) or not CHAT_ID_PATTERN.fullmatch(video_id):
                        raise ImportError("Invalid video ID in legacy owner store")
                    if not isinstance(username, str) or username.casefold() not in user_ids:
                        raise ImportError(f"Video owner has no imported user for video {video_id}")
                    existing = session.get(Video, video_id)
                    user_id = user_ids[username.casefold()]
                    if existing is None:
                        session.add(
                            Video(
                                id=video_id,
                                user_id=user_id,
                                source_type="upload",
                                status="completed",
                            )
                        )
                        session.flush()
                        counts["videos"] += 1
                    elif existing.user_id != user_id:
                        raise ImportError(f"Video {video_id} is already assigned to another user")

                for chat_path in chat_files:
                    raw = _read_json(chat_path)
                    try:
                        record = ChatRecord.model_validate(raw)
                    except Exception as exc:
                        raise ImportError(f"Invalid legacy chat record in {chat_path}") from exc
                    if not CHAT_ID_PATTERN.fullmatch(record.chat_id):
                        raise ImportError(f"Invalid chat ID in {chat_path}")
                    username = record.username.casefold()
                    user_id = user_ids.get(username)
                    if user_id is None:
                        existing_user = session.scalar(select(User).where(User.username == username))
                        if existing_user is None:
                            raise ImportError(f"Chat has no imported user: {chat_path}")
                        user_id = existing_user.id
                        user_ids[username] = user_id

                    existing_chat = session.get(Chat, record.chat_id)
                    if existing_chat is not None:
                        if existing_chat.user_id != user_id:
                            raise ImportError(f"Chat {record.chat_id} is already assigned to another user")
                        continue

                    video_id = record.video_id
                    if video_id:
                        video = session.get(Video, video_id)
                        if video is None or video.user_id != user_id:
                            raise ImportError(
                                f"Chat {record.chat_id} references a video not owned by the same user"
                            )
                    chat = Chat(
                        id=record.chat_id,
                        user_id=user_id,
                        video_id=video_id,
                        title=record.title,
                        created_at=record.created_at,
                        updated_at=record.updated_at,
                        video_metadata=record.video_metadata,
                        summary=record.summary,
                        transcript=record.transcript,
                    )
                    session.add(chat)
                    session.flush()
                    counts["chats"] += 1
                    for message in record.messages:
                        stored = Message(
                            id=message.id,
                            chat_id=record.chat_id,
                            role=message.role,
                            content=message.content,
                            created_at=message.timestamp,
                            answer_audio_ref=message.answer_audio_ref,
                            timestamps=message.timestamps,
                        )
                        session.add(stored)
                        session.flush()
                        counts["messages"] += 1
                        for position, source in enumerate(message.sources):
                            session.add(
                                MessageSource(
                                    message_id=message.id,
                                    position=position,
                                    text=source.text,
                                    start_s=source.start,
                                    end_s=source.end,
                                )
                            )
                            counts["message_sources"] += 1

                if dry_run:
                    transaction.rollback()
                else:
                    transaction.commit()
            except Exception:
                transaction.rollback()
                raise
    except SQLAlchemyError as exc:
        raise ImportError("Database import failed; no JSON source files were changed") from exc
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import legacy JSON users, video ownership, and chats into SQL storage.")
    parser.add_argument("--dry-run", action="store_true", help="Validate and count imports, then roll back all changes.")
    parser.add_argument("--data-dir", type=Path, default=settings.project_root / "data")
    parser.add_argument("--database-url", default=settings.database_url)
    args = parser.parse_args(argv)
    database = Database(args.database_url, settings.sqlite_busy_timeout_ms)
    try:
        result = import_json_data(args.data_dir, database, dry_run=args.dry_run)
        action = "Would import" if args.dry_run else "Imported"
        print(f"{action} " + ", ".join(f"{count} {name}" for name, count in result.items()))
        return 0
    except ImportError as exc:
        print(f"JSON import failed: {exc}", file=sys.stderr)
        return 1
    finally:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
