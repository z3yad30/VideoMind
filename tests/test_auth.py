from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import backend.app.api.auth as auth_api
import backend.app.api.dependencies as auth_dependencies
from backend.app.main import app
from backend.app.db.models import User
from backend.app.repositories.user import UserRepo
from backend.app.services.auth import AuthenticationService
from backend.app.services.user_storage import UserStore
from tests.database import create_database


@pytest.fixture
def isolated_auth_service(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    database = create_database(tmp_path)
    service = AuthenticationService(UserStore(UserRepo(database.session_factory)))
    monkeypatch.setattr(auth_api, "auth_service", service)
    monkeypatch.setattr(auth_dependencies, "auth_service", service)
    yield service, database
    database.dispose()


@pytest.mark.asyncio
async def test_register_persists_password_hash_without_returning_it(isolated_auth_service) -> None:
    service, database = isolated_auth_service
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/auth/register", json={"username": "Alice", "password": "correct horse"})

    assert response.status_code == 201
    assert response.json() == {"username": "alice"}
    with database.session_factory() as session:
        record = session.scalar(select(User).where(User.username == "alice"))
        assert record.password_hash != "correct horse"
        assert service.user_store.find("alice")["password_hash"] == record.password_hash
    assert "password_hash" not in response.text


@pytest.mark.asyncio
async def test_duplicate_registration_is_rejected(isolated_auth_service) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/auth/register", json={"username": "Alice", "password": "first password"})
        response = await client.post("/auth/register", json={"username": " alice ", "password": "second password"})

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_login_and_authenticated_me(isolated_auth_service) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/auth/register", json={"username": "alice", "password": "correct horse"})
        login_response = await client.post("/auth/login", json={"username": "alice", "password": "correct horse"})
        me_response = await client.get("/auth/me")

    assert login_response.status_code == 200
    assert login_response.json() == {"username": "alice"}
    cookie = login_response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert me_response.status_code == 200
    assert me_response.json() == {"username": "alice"}


@pytest.mark.asyncio
async def test_login_failures_use_same_response(isolated_auth_service) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/auth/register", json={"username": "alice", "password": "correct horse"})
        invalid_password = await client.post("/auth/login", json={"username": "alice", "password": "wrong"})
        unknown_user = await client.post("/auth/login", json={"username": "unknown", "password": "wrong"})

    assert invalid_password.status_code == unknown_user.status_code == 401
    assert invalid_password.json() == unknown_user.json()


@pytest.mark.asyncio
async def test_me_rejects_unauthenticated_request(isolated_auth_service) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/auth/me")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_logout_clears_and_invalidates_session(isolated_auth_service) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/auth/register", json={"username": "alice", "password": "correct horse"})
        await client.post("/auth/login", json={"username": "alice", "password": "correct horse"})
        session_cookie = client.cookies.get("videomind_session")
        logout_response = await client.post("/auth/logout")
        client.cookies.set("videomind_session", session_cookie)
        me_response = await client.get("/auth/me")

    assert logout_response.status_code == 204
    assert "videomind_session" not in logout_response.headers.get("set-cookie", "") or "max-age=0" in logout_response.headers["set-cookie"].lower()
    assert me_response.status_code == 401


@pytest.mark.asyncio
async def test_registration_rejects_malformed_credentials(isolated_auth_service) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        empty_username = await client.post("/auth/register", json={"username": "   ", "password": "password"})
        empty_password = await client.post("/auth/register", json={"username": "alice", "password": "   "})
        extra_field = await client.post("/auth/register", json={"username": "alice", "password": "pw", "extra": True})

    assert empty_username.status_code == empty_password.status_code == extra_field.status_code == 422


@pytest.mark.asyncio
async def test_database_failure_returns_service_unavailable(isolated_auth_service) -> None:
    service, database = isolated_auth_service
    with database.engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE users")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/auth/login", json={"username": "alice", "password": "password"})

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_corrupted_password_hash_returns_service_unavailable(isolated_auth_service) -> None:
    _service, database = isolated_auth_service
    with database.session_factory.begin() as session:
        session.add(User(id="a" * 32, username="alice", password_hash="broken-hash"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/auth/login", json={"username": "alice", "password": "password"})

    assert response.status_code == 503
    assert response.json() == {"detail": "User storage is unavailable"}