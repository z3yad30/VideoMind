from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update
import subprocess

import backend.app.api.auth as auth_api
from backend.app.core.csrf import is_allowed_request_origin
from backend.app.db.models import Session
from backend.app.core.rate_limit import rate_limiter
from backend.app.main import app
from backend.app.repositories.user import UserRepo
from backend.app.repositories.video import VideoRepo
from backend.app.services.auth import AuthenticationService
from backend.app.services.media import MediaProcessingError, MediaService
from backend.app.services.user_storage import UserStore
from backend.app.services.video_storage import VideoOwnershipStore
from tests.database import create_database


def test_csrf_requires_a_configured_origin() -> None:
    assert not is_allowed_request_origin(None, None)
    assert not is_allowed_request_origin("https://evil.example", None)
    assert is_allowed_request_origin(None, "http://localhost:5173/app")


@pytest.mark.asyncio
async def test_csrf_middleware_rejects_disallowed_origin() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={"username": "alice", "password": "password"},
            headers={"Origin": "https://evil.example"},
        )
        missing = await client.post(
            "/auth/login",
            json={"username": "alice", "password": "password"},
            headers={"Origin": ""},
        )
    assert response.status_code == 403
    assert missing.status_code == 403


@pytest.mark.asyncio
async def test_authentication_rate_limit_returns_429(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_api.auth_service, "authenticate", lambda *_: None)
    rate_limiter.reset()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        responses = [
            await client.post("/auth/login", json={"username": "limited", "password": "password"})
            for _ in range(6)
        ]
    assert [response.status_code for response in responses][-1] == 429


def test_upload_magic_bytes_must_match_supported_media(tmp_path: Path) -> None:
    path = tmp_path / "fake.mp4"
    path.write_bytes(b"this is not a video")
    with pytest.raises(MediaProcessingError, match="not a supported media format"):
        MediaService().validate_magic(path)


def test_expired_sessions_can_be_purged_and_sessions_survive_service_recreation(tmp_path: Path) -> None:
    database = create_database(tmp_path)
    service = AuthenticationService(UserStore(UserRepo(database.session_factory)))
    service.register("alice", "valid-password")
    user = service.authenticate("alice", "valid-password")
    assert user is not None
    token = service.create_session(user)
    restored = AuthenticationService(UserStore(UserRepo(database.session_factory)))
    assert restored.get_session_user(token).username == "alice"

    with database.session_factory.begin() as session:
        session.execute(
            update(Session).values(expires_at=datetime(2000, 1, 1, tzinfo=timezone.utc))
        )
    assert restored.user_store.purge_expired_sessions() == 1
    assert restored.get_session_user(token) is None
    database.dispose()


def test_media_duration_probe_uses_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append(kwargs["timeout"])
        return SimpleNamespace(returncode=0, stdout="12.5", stderr="")

    monkeypatch.setattr("backend.app.services.media.subprocess.run", fake_run)
    assert MediaService().get_duration(tmp_path / "source.mp4") == 12.5
    assert calls


def test_media_duration_probe_rejects_over_limit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "backend.app.services.media.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="90000", stderr=""),
    )
    with pytest.raises(MediaProcessingError, match="maximum allowed duration"):
        MediaService().get_duration(tmp_path / "source.mp4")


def test_youtube_subprocess_timeout_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr("backend.app.services.media.subprocess.run", timeout)
    with pytest.raises(MediaProcessingError, match="YouTube operation timed out"):
        MediaService().download_youtube("https://youtube.com/watch?v=abc", tmp_path)


def test_youtube_validator_rejects_noncanonical_hosts() -> None:
    with pytest.raises(MediaProcessingError, match="Only YouTube URLs"):
        MediaService.validate_youtube_url("https://m.youtube.com/watch?v=abc")


def test_youtube_duration_uses_supported_youtube_extractor_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout='{"duration": 12.5}', stderr="")

    monkeypatch.setattr("backend.app.services.media.subprocess.run", fake_run)

    assert MediaService().get_youtube_duration("https://youtube.com/watch?v=abc") == 12.5
    assert "--use-extractors" in commands[0]
    assert "youtube" in commands[0]
    assert "--allowed-extractors" not in commands[0]


def test_video_and_duration_quotas_use_persisted_video_durations(tmp_path: Path) -> None:
    database = create_database(tmp_path)
    user_service = AuthenticationService(UserStore(UserRepo(database.session_factory)))
    user_service.register("alice", "valid-password")
    ownership = VideoOwnershipStore(VideoRepo(database.session_factory))
    ownership.register("a" * 32, "alice", duration_s=300)

    assert not ownership.check_quota("alice", 1, max_videos=1, max_total_minutes=10)
    assert not ownership.check_quota("alice", 301, max_videos=10, max_total_minutes=10)
    assert ownership.check_quota("alice", 299, max_videos=10, max_total_minutes=10)
    database.dispose()
