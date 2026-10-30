import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

import backend.app.api.health as health
from backend.app.main import app
from backend.app.db.database import Database


@pytest.mark.asyncio
async def test_health_endpoint() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health", headers={"X-Request-ID": "health-test-123"})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Request-ID"] == "health-test-123"


@pytest.mark.parametrize("failure", ["database", "ffmpeg", "data_dir_writable", "groq_api_key_configured"])
def test_readiness_reports_dependency_failures(
    failure: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database = Database("sqlite:///:memory:")
    monkeypatch.setattr(health, "get_database", lambda: database)
    monkeypatch.setattr(health.shutil, "which", lambda _binary: "ffmpeg.exe")
    monkeypatch.setattr(
        health,
        "settings",
        SimpleNamespace(
            project_root=tmp_path,
            ffmpeg_binary="ffmpeg",
            groq_api_key="configured-test-key",
        ),
    )

    if failure == "database":
        from sqlalchemy.exc import SQLAlchemyError

        monkeypatch.setattr(health, "get_database", lambda: (_ for _ in ()).throw(SQLAlchemyError("offline")))
    elif failure == "ffmpeg":
        monkeypatch.setattr(health.shutil, "which", lambda _binary: None)
    elif failure == "data_dir_writable":
        data_dir.rmdir()
    else:
        health.settings.groq_api_key = ""

    response = health.readiness_check()
    checks = json.loads(response.body)["checks"]

    assert response.status_code == 503
    assert checks[failure] is False
    database.dispose()


def test_readiness_succeeds_when_dependencies_are_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "data").mkdir()
    database = Database("sqlite:///:memory:")
    monkeypatch.setattr(health, "get_database", lambda: database)
    monkeypatch.setattr(health.shutil, "which", lambda _binary: "ffmpeg.exe")
    monkeypatch.setattr(
        health,
        "settings",
        SimpleNamespace(
            project_root=tmp_path,
            ffmpeg_binary="ffmpeg",
            groq_api_key="configured-test-key",
        ),
    )

    response = health.readiness_check()

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "status": "ok",
        "checks": {
            "database": True,
            "ffmpeg": True,
            "data_dir_writable": True,
            "groq_api_key_configured": True,
        },
    }
    database.dispose()
