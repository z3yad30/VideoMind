import json
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

import backend.app.api.auth as auth_api
import backend.app.api.chats as chats_api
import backend.app.api.dependencies as auth_dependencies
from backend.app.main import app
from backend.app.services.auth import AuthenticationService
from backend.app.services.chat_storage import ChatStore
from backend.app.services.user_storage import UserStore


@pytest.fixture
def isolated_services(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ChatStore:
    auth_service = AuthenticationService(UserStore(tmp_path / "users" / "users.json"))
    chat_service = ChatStore(tmp_path / "data" / "chats")
    monkeypatch.setattr(auth_api, "auth_service", auth_service)
    monkeypatch.setattr(auth_dependencies, "auth_service", auth_service)
    monkeypatch.setattr(chats_api, "chat_store", chat_service)
    return chat_service


async def register_and_login(client: AsyncClient, username: str) -> None:
    response = await client.post("/auth/register", json={"username": username, "password": "test password"})
    assert response.status_code == 201
    response = await client.post("/auth/login", json={"username": username, "password": "test password"})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_chat_crud_and_list(isolated_services: ChatStore) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await register_and_login(client, "alice")
        created = await client.post("/chats", json={"messages": [{"role": "user", "content": "Explain this video"}]})
        chat = created.json()
        chat_id = chat["chat_id"]
        listed = await client.get("/chats")
        updated = await client.put("/chats/" + chat_id, json={
            "title": "Video notes",
            "messages": [
                {"id": "question-1", "role": "user", "content": "Explain this video"},
                {"id": "answer-1", "role": "assistant", "content": "A grounded answer", "sources": [{"start": 4.0, "end": 8.0, "text": "Evidence"}], "timestamps": [4.0, 8.0], "answer_audio_ref": "/videos/video1/answers/answer1/audio"},
            ],
        })
        retrieved = await client.get("/chats/" + chat_id)
        deleted = await client.delete("/chats/" + chat_id)
        missing = await client.get("/chats/" + chat_id)

    assert created.status_code == 201
    assert chat["username"] == "alice"
    assert chat["title"] == "Explain this video"
    assert listed.status_code == 200 and len(listed.json()) == 1
    assert "messages" not in listed.json()[0]
    assert updated.status_code == 200 and updated.json()["title"] == "Video notes"
    assert updated.json()["messages"][1]["sources"][0]["start"] == 4.0
    assert retrieved.json()["messages"][1]["answer_audio_ref"].endswith("/audio")
    assert deleted.status_code == 204
    assert missing.status_code == 404
    assert list((isolated_services.root / "alice").glob("*.json")) == []


@pytest.mark.asyncio
async def test_chat_persists_after_store_reload(isolated_services: ChatStore) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await register_and_login(client, "alice")
        created = await client.post("/chats", json={"video_id": "video1", "messages": [{"role": "assistant", "content": "Saved reply"}]})
        chat_id = created.json()["chat_id"]

    reloaded_store = ChatStore(isolated_services.root)
    record = reloaded_store.get("alice", chat_id)
    assert record.messages[0].content == "Saved reply"
    stored_file = isolated_services.root / "alice" / f"{chat_id}.json"
    assert json.loads(stored_file.read_text(encoding="utf-8"))["username"] == "alice"


@pytest.mark.asyncio
async def test_chat_detail_restores_canonical_video_context(isolated_services: ChatStore) -> None:
    data_root = isolated_services.data_root
    (data_root / "transcripts").mkdir(parents=True)
    (data_root / "summaries").mkdir(parents=True)
    (data_root / "transcripts" / "video1.json").write_text(json.dumps({"video_id": "video1", "segments": [{"text": "Transcript", "start": 1, "end": 2}]}), encoding="utf-8")
    (data_root / "summaries" / "video1.json").write_text(json.dumps({"video_id": "video1", "summary": {"Overview": "A summary"}}), encoding="utf-8")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await register_and_login(client, "alice")
        created = await client.post("/chats", json={"video_id": "video1"})
        restored = await client.get("/chats/" + created.json()["chat_id"])

    assert restored.json()["summary"] == {"Overview": "A summary"}
    assert restored.json()["transcript"]["segments"][0]["text"] == "Transcript"
    saved = json.loads((isolated_services.root / "alice" / f"{created.json()['chat_id']}.json").read_text(encoding="utf-8"))
    assert saved["transcript"] is None


@pytest.mark.asyncio
async def test_users_cannot_read_update_or_delete_another_users_chat(isolated_services: ChatStore) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as alice, AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as bob:
        await register_and_login(alice, "alice")
        await register_and_login(bob, "bob")
        created = await alice.post("/chats", json={"title": "Alice's chat"})
        chat_id = created.json()["chat_id"]
        read = await bob.get("/chats/" + chat_id)
        update = await bob.put("/chats/" + chat_id, json={"title": "stolen"})
        delete = await bob.delete("/chats/" + chat_id)
        still_owned = await alice.get("/chats/" + chat_id)

    assert read.status_code == update.status_code == delete.status_code == 404
    assert still_owned.status_code == 200
    assert still_owned.json()["title"] == "Alice's chat"


@pytest.mark.asyncio
async def test_chat_username_cannot_be_supplied_by_client(isolated_services: ChatStore) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await register_and_login(client, "alice")
        response = await client.post("/chats", json={"username": "bob", "title": "Spoofed"})

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_malformed_chat_json_returns_service_unavailable(isolated_services: ChatStore) -> None:
    chat_id = uuid4().hex
    user_dir = isolated_services.root / "alice"
    user_dir.mkdir(parents=True)
    (user_dir / f"{chat_id}.json").write_text("{broken", encoding="utf-8")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await register_and_login(client, "alice")
        response = await client.get("/chats/" + chat_id)

    assert response.status_code == 503
    assert "data" not in response.text


@pytest.mark.asyncio
async def test_invalid_and_path_traversal_chat_ids_are_rejected(isolated_services: ChatStore, tmp_path: Path) -> None:
    outside = tmp_path / "users.json"
    outside.write_text("private", encoding="utf-8")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await register_and_login(client, "alice")
        invalid = await client.get("/chats/not-a-chat-id")
        traversal = await client.get("/chats/%2e%2e%2fusers.json")

    assert invalid.status_code == 422
    assert traversal.status_code in {404, 422}
    assert outside.read_text(encoding="utf-8") == "private"