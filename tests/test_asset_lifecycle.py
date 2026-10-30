import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from backend.app.api.dependencies import get_current_user
from backend.app.adapters.sql_job_queue import SqlJobQueue
from backend.app.container import (
    get_blob_store,
    get_media_service,
    get_vector_store,
    get_video_processing_service,
)
from backend.app.main import app
from backend.app.repositories.chat import ChatRepo
from backend.app.repositories.video import VideoRepo
from backend.app.services.auth import AuthenticatedUser
from backend.app.services.chat_storage import ChatStore
from backend.app.services.video_processing import VideoProcessingService
from backend.app.services.video_storage import VideoOwnershipStore
from tests.database import create_database, seed_user
from tests.fakes import FakeBlobStore, FakeTranscriber, FakeVectorStore
import backend.app.api.account as account_api
import backend.app.api.chats as chats_api
import backend.app.api.videos as videos_api


@pytest.fixture
def lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    database = create_database(tmp_path)
    seed_user(database, "alice")
    seed_user(database, "bob")
    seed_user(database, "carol")
    ownership = VideoOwnershipStore(VideoRepo(database.session_factory))
    blob_store = FakeBlobStore(tmp_path / "data")
    vector_store = FakeVectorStore()
    queue = SqlJobQueue(database.session_factory)
    media = SimpleNamespace(
        validate_filename=lambda _filename: ".mp4",
        get_duration=lambda _path: 12.0,
    )

    def save_upload_with_digest(upload, destination: Path):
        digest = hashlib.sha256()
        size = 0
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as target:
            while chunk := upload.file.read(1024 * 1024):
                target.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        return SimpleNamespace(sha256=digest.hexdigest(), size=size)

    media.save_upload_with_digest = save_upload_with_digest
    service = VideoProcessingService(media, FakeTranscriber(), None, blob_store, queue)
    chats = ChatStore(ChatRepo(database.session_factory), data_root=tmp_path / "data")
    user = {"name": "alice"}

    monkeypatch.setattr(videos_api, "video_ownership", ownership)
    monkeypatch.setattr(chats_api, "video_ownership", ownership)
    monkeypatch.setattr(account_api, "video_ownership", ownership)
    monkeypatch.setattr(chats_api, "chat_store", chats)
    monkeypatch.setitem(app.dependency_overrides, get_current_user, lambda: AuthenticatedUser(user["name"]))
    monkeypatch.setitem(app.dependency_overrides, get_video_processing_service, lambda: service)
    monkeypatch.setitem(app.dependency_overrides, get_media_service, lambda: media)
    monkeypatch.setitem(app.dependency_overrides, get_blob_store, lambda: blob_store)
    monkeypatch.setitem(app.dependency_overrides, get_vector_store, lambda: vector_store)
    monkeypatch.setattr(
        videos_api,
        "_start_processing",
        lambda processing_service, video_id: processing_service.enqueue(video_id),
    )
    yield SimpleNamespace(
        database=database,
        ownership=ownership,
        blob_store=blob_store,
        vector_store=vector_store,
        queue=queue,
        service=service,
        chats=chats,
        user=user,
    )
    database.dispose()


async def _create_chat(client: AsyncClient, video_id: str) -> str:
    response = await client.post(
        "/chats",
        json={
            "video_id": video_id,
            "messages": [{"role": "user", "content": "My video"}],
        },
        headers={"Origin": "http://localhost:5173"},
    )
    assert response.status_code == 201
    return response.json()["chat_id"]


@pytest.mark.asyncio
async def test_shared_uploads_keep_video_ownership_and_cleanup_after_last_delete(lifecycle) -> None:
    origin = {"Origin": "http://localhost:5173"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        alice_upload = await client.post(
            "/videos/upload",
            files={"file": ("source.mp4", b"identical media bytes", "video/mp4")},
            headers=origin,
        )
        assert alice_upload.status_code == 202
        alice_video = alice_upload.json()["video_id"]

        lifecycle.user["name"] = "bob"
        bob_upload = await client.post(
            "/videos/upload",
            files={"file": ("copy.mp4", b"identical media bytes", "video/mp4")},
            headers=origin,
        )
        assert bob_upload.status_code == 202
        bob_video = bob_upload.json()["video_id"]
        assert bob_upload.json()["status"] == "queued"
        claimed = lifecycle.queue.claim_next("worker")
        assert claimed is not None and claimed.video_id == alice_video
        assert lifecycle.queue.claim_next("other-worker") is None

        assert lifecycle.queue.complete(claimed.id, "worker")
        lifecycle.user["name"] = "carol"
        carol_upload = await client.post(
            "/videos/upload",
            files={"file": ("third-copy.mp4", b"identical media bytes", "video/mp4")},
            headers=origin,
        )
        assert carol_upload.status_code == 202
        carol_video = carol_upload.json()["video_id"]
        assert carol_upload.json()["status"] == "completed"
        assert lifecycle.queue.claim_next("worker") is None
        lifecycle.user["name"] = "bob"
        assert (await client.get(f"/videos/{bob_video}")).json()["video_id"] == bob_video
        assert lifecycle.queue.get_job(alice_video).state == "completed"
        assert lifecycle.queue.get_job(bob_video).state == "completed"

        bob_chat = await _create_chat(client, bob_video)
        assert (await client.get(f"/videos/{alice_video}")).status_code == 404
        assert (await client.get(f"/chats/{bob_chat}")).status_code == 200
        lifecycle.user["name"] = "carol"
        carol_chat = await _create_chat(client, carol_video)
        assert (await client.get(f"/chats/{bob_chat}")).status_code == 404

        lifecycle.user["name"] = "alice"
        alice_chat = await _create_chat(client, alice_video)
        assert (await client.get(f"/videos/{bob_video}")).status_code == 404
        assert (await client.get(f"/chats/{bob_chat}")).status_code == 404
        asset_id = lifecycle.ownership.asset_id_for_video(alice_video, "alice")
        assert asset_id
        blob_key = f"videos/assets/{asset_id}.mp4"
        lifecycle.blob_store.put(f"transcripts/{asset_id}.json", "{}")
        lifecycle.blob_store.put(f"summaries/{asset_id}.json", "{}")
        lifecycle.blob_store.put(f"audio/summaries/{asset_id}.wav", b"summary audio")
        lifecycle.blob_store.put(f"audio/answers/{asset_id}/answer.wav", b"answer audio")
        lifecycle.vector_store.upsert(asset_id, [{"id": "chunk"}])

        deleted = await client.delete(f"/videos/{alice_video}", headers=origin)
        assert deleted.status_code == 204
        lifecycle.user["name"] = "bob"
        retained = await client.get(f"/videos/{bob_video}/media")
        assert retained.status_code == 200
        assert retained.content == b"identical media bytes"
        assert lifecycle.blob_store.exists(blob_key)
        assert asset_id in lifecycle.vector_store.chunks
        lifecycle.user["name"] = "alice"
        removed_chat = await client.get(f"/chats/{alice_chat}")
        assert removed_chat.status_code == 200
        assert removed_chat.json()["video_id"] is None
        assert removed_chat.json()["video_missing"] is True

        lifecycle.user["name"] = "bob"
        deleted = await client.delete(f"/videos/{bob_video}", headers=origin)
        assert deleted.status_code == 204
        assert lifecycle.blob_store.exists(blob_key)
        lifecycle.user["name"] = "carol"
        deleted = await client.delete(f"/videos/{carol_video}", headers=origin)
        assert deleted.status_code == 204
        assert not lifecycle.blob_store.exists(blob_key)
        assert not lifecycle.blob_store.exists(f"transcripts/{asset_id}.json")
        assert not lifecycle.blob_store.exists(f"summaries/{asset_id}.json")
        assert not lifecycle.blob_store.exists(f"audio/summaries/{asset_id}.wav")
        assert not lifecycle.blob_store.exists(f"audio/answers/{asset_id}/answer.wav")
        assert asset_id not in lifecycle.vector_store.chunks
        lifecycle.user["name"] = "bob"
        missing_chat = await client.get(f"/chats/{bob_chat}")
        assert missing_chat.status_code == 200
        assert missing_chat.json()["video_missing"] is True
        lifecycle.user["name"] = "carol"
        assert (await client.get(f"/chats/{carol_chat}")).json()["video_missing"] is True


@pytest.mark.asyncio
async def test_delete_my_data_preserves_assets_shared_with_another_user(lifecycle) -> None:
    asset = lifecycle.ownership.register_upload(
        "1" * 32,
        "alice",
        "b" * 64,
        10,
        12.0,
        ".mp4",
    )
    duplicate = lifecycle.ownership.register_upload(
        "2" * 32,
        "bob",
        "b" * 64,
        10,
        12.0,
        ".mp4",
    )
    assert asset.asset_id == duplicate.asset_id
    lifecycle.blob_store.put(asset.blob_key, b"shared")
    lifecycle.queue.enqueue("1" * 32)
    lifecycle.user["name"] = "bob"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        chat_id = await _create_chat(client, "2" * 32)
        lifecycle.user["name"] = "alice"
        response = await client.delete("/me/data", headers={"Origin": "http://localhost:5173"})
        assert response.status_code == 204
        assert lifecycle.blob_store.exists(asset.blob_key)
        assert lifecycle.ownership.is_owned_by("2" * 32, "bob")
        surviving_job = lifecycle.queue.get_job("2" * 32)
        assert surviving_job is not None
        assert surviving_job.video_id == "2" * 32
        assert (await client.get(f"/chats/{chat_id}")).status_code == 404


@pytest.mark.asyncio
async def test_delete_youtube_video_cleans_video_keyed_artifacts(lifecycle) -> None:
    video_id = "4" * 32
    lifecycle.ownership.register(video_id, "alice", source_type="youtube")
    lifecycle.blob_store.put(f"videos/{video_id}.mp4", b"youtube media")
    for key in (
        f"audio/extracted/{video_id}.wav",
        f"transcripts/{video_id}.json",
        f"summaries/{video_id}.json",
        f"audio/summaries/{video_id}.wav",
        f"audio/answers/{video_id}/answer.wav",
    ):
        lifecycle.blob_store.put(key, b"artifact")
    lifecycle.vector_store.upsert(video_id, [{"id": "chunk"}])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.delete(
            f"/videos/{video_id}",
            headers={"Origin": "http://localhost:5173"},
        )

    assert response.status_code == 204
    assert not lifecycle.blob_store.exists(f"videos/{video_id}.mp4")
    assert not lifecycle.blob_store.exists(f"transcripts/{video_id}.json")
    assert not lifecycle.blob_store.exists(f"summaries/{video_id}.json")
    assert not lifecycle.blob_store.exists(f"audio/extracted/{video_id}.wav")
    assert not lifecycle.blob_store.exists(f"audio/summaries/{video_id}.wav")
    assert not lifecycle.blob_store.exists(f"audio/answers/{video_id}/answer.wav")
    assert video_id not in lifecycle.vector_store.chunks
