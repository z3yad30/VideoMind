import asyncio
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
                {"id": "answer-1", "role": "assistant", "content": "A grounded answer", "sources": [{"start": 4.0, "end": 8.0, "text": "Evidence"}], "timestamps": [4.0, 8.0]},
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
    assert retrieved.json()["messages"][1]["answer_audio_ref"] is None
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
async def test_chat_question_persists_both_messages_sources_audio_and_reload(
    isolated_services: ChatStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    class AI:
        def __init__(self) -> None:
            self.calls = []

        def answer_question(self, video_id, question, *, history):
            self.calls.append((video_id, question, history))
            return {"answer": "A grounded answer", "sources": [{"start": 4.0, "end": 8.0, "text": "Evidence"}]}

    ai = AI()
    monkeypatch.setattr(chats_api, "ai_service", ai)
    class Videos:
        def get_job(self, video_id):
            return type("Job", (), {"status": "completed"})()

    class Voice:
        def synthesize_answer(self, video_id, answer):
            return "a" * 32, Path("answer.wav")

    monkeypatch.setattr(chats_api, "video_service", Videos())
    monkeypatch.setattr(chats_api, "voice_service", Voice())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await register_and_login(client, "alice")
        created = await client.post("/chats", json={
            "video_id": "video-1",
            "messages": [
                {"role": "user", "content": f"Prior question {index}"}
                for index in range(5)
            ],
        })
        chat_id = created.json()["chat_id"]
        response = await client.post(f"/chats/{chat_id}/messages", json={"question": "Explain this concept"})
        restored = await client.get(f"/chats/{chat_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["user_message"]["content"] == "Explain this concept"
    assert payload["assistant_message"]["content"] == "A grounded answer"
    assert payload["assistant_message"]["sources"] == [{"start": 4.0, "end": 8.0, "text": "Evidence"}]
    assert payload["assistant_message"]["timestamps"] == [4.0, 8.0]
    expected_audio_ref = f"/chats/{chat_id}/messages/{payload['assistant_message']['id']}/audio/{'a' * 32}"
    assert payload["assistant_message"]["answer_audio_ref"] == expected_audio_ref
    assert payload["answer_audio_location"] == expected_audio_ref
    assert len(restored.json()["messages"]) == 7
    assert restored.json()["messages"][-1]["content"] == "A grounded answer"
    assert ai.calls[0] == ("video-1", "Explain this concept", [
        {"role": "user", "content": "Prior question 2"},
        {"role": "user", "content": "Prior question 3"},
        {"role": "user", "content": "Prior question 4"},
    ])
    reloaded = ChatStore(isolated_services.root).get("alice", chat_id)
    assert reloaded.messages[-1].sources[0].start == 4.0
    assert reloaded.messages[-1].answer_audio_ref == expected_audio_ref


@pytest.mark.asyncio
async def test_chat_answer_audio_generation_is_idempotent_persisted_and_owner_scoped(
    isolated_services: ChatStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    answer_id = "b" * 32

    class Videos:
        def get_job(self, video_id):
            return type("Job", (), {"status": "completed"})()

    class Voice:
        def __init__(self) -> None:
            self.audio_dir = tmp_path / "answers"
            self.calls = 0

        def synthesize_answer(self, video_id, answer):
            self.calls += 1
            audio_path = self.audio_dir / video_id / f"{answer_id}.wav"
            audio_path.parent.mkdir(parents=True, exist_ok=True)
            audio_path.write_bytes(b"wav-audio")
            return answer_id, audio_path

    voice = Voice()
    monkeypatch.setattr(chats_api, "video_service", Videos())
    monkeypatch.setattr(chats_api, "voice_service", voice)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as alice, AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as bob:
        await register_and_login(alice, "alice")
        await register_and_login(bob, "bob")
        created = await alice.post("/chats", json={
            "video_id": "c" * 32,
            "messages": [{"role": "assistant", "content": "A saved answer"}],
        })
        chat_id = created.json()["chat_id"]
        message_id = created.json()["messages"][0]["id"]
        audio_path = f"/chats/{chat_id}/messages/{message_id}/audio"
        first, repeated = await asyncio.gather(alice.post(audio_path), alice.post(audio_path))
        restored = await alice.get(f"/chats/{chat_id}")
        played = await alice.get(audio_path)
        cached = await alice.post(audio_path)
        foreign_playback = await bob.get(audio_path)
        foreign_generation = await bob.post(audio_path)
        (voice.audio_dir / ("c" * 32) / f"{answer_id}.wav").unlink()
        recovered = await alice.post(audio_path)

    expected_ref = f"{audio_path}/{answer_id}"
    assert first.status_code == repeated.status_code == 200
    assert first.json()["answer_audio_ref"] == repeated.json()["answer_audio_ref"] == expected_ref
    assert cached.status_code == recovered.status_code == 200
    assert voice.calls == 2
    assert restored.json()["messages"][0]["answer_audio_ref"] == expected_ref
    assert played.status_code == 200 and played.content == b"wav-audio"
    assert foreign_playback.status_code == foreign_generation.status_code == 404


@pytest.mark.asyncio
async def test_chat_question_requires_an_available_video(isolated_services: ChatStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chats_api, "video_service", type("Videos", (), {"get_job": lambda self, _: None})())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await register_and_login(client, "alice")
        no_video = await client.post("/chats", json={})
        no_video_question = await client.post(
            f"/chats/{no_video.json()['chat_id']}/messages", json={"question": "Question"}
        )
        missing_video = await client.post("/chats", json={"video_id": "missing-video"})
        missing_video_question = await client.post(
            f"/chats/{missing_video.json()['chat_id']}/messages", json={"question": "Question"}
        )

    assert no_video_question.status_code == 400
    assert no_video_question.json()["detail"] == "Chat is not associated with a video"
    assert missing_video_question.status_code == 404
    assert missing_video_question.json()["detail"] == "Video not found"


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
        ask = await bob.post("/chats/" + chat_id + "/messages", json={"question": "Question"})
        delete = await bob.delete("/chats/" + chat_id)
        still_owned = await alice.get("/chats/" + chat_id)

    assert read.status_code == update.status_code == ask.status_code == delete.status_code == 404
    assert still_owned.status_code == 200
    assert still_owned.json()["title"] == "Alice's chat"


@pytest.mark.asyncio
async def test_chat_username_cannot_be_supplied_by_client(isolated_services: ChatStore) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await register_and_login(client, "alice")
        response = await client.post("/chats", json={"username": "bob", "title": "Spoofed"})

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_chat_clients_cannot_inject_answer_audio_references(isolated_services: ChatStore) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await register_and_login(client, "alice")
        created = await client.post("/chats", json={"video_id": "a" * 32})
        chat_id = created.json()["chat_id"]
        forged_create = await client.post("/chats", json={
            "messages": [{"role": "assistant", "content": "Forged", "answer_audio_ref": "/chats/other/messages/id/audio/" + "b" * 32}],
        })
        forged_update = await client.put(f"/chats/{chat_id}", json={
            "messages": [{"role": "assistant", "content": "Forged", "answer_audio_ref": "/chats/other/messages/id/audio/" + "b" * 32}],
        })

    assert forged_create.status_code == forged_update.status_code == 422


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