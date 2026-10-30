import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from backend.app.main import app
from backend.app.api.dependencies import get_current_user
from backend.app.container import get_ai_service, get_blob_store, get_media_service, get_video_processing_service
from backend.app.repositories.video import VideoRepo
from backend.app.services.auth import AuthenticatedUser
from backend.app.services.asr import ASRSegment
from backend.app.services.media import MediaService
from backend.app.services.video_processing import VideoProcessingService
from backend.app.schemas.videos import YouTubeRequest
import backend.app.api.videos as videos_api
import backend.app.services.media as media_module
from backend.app.services.video_storage import VideoOwnershipStore
from tests.fakes import FakeBlobStore, FakeJobQueue, FakeTranscriber
from tests.database import create_database, seed_user


@pytest.fixture(autouse=True)
def authenticated_video_user(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> VideoOwnershipStore:
    database = create_database(tmp_path)
    seed_user(database, "alice")
    seed_user(database, "bob")
    owners = VideoOwnershipStore(VideoRepo(database.session_factory))
    monkeypatch.setattr(videos_api, "video_ownership", owners)
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser("alice")
    yield owners
    app.dependency_overrides.pop(get_current_user, None)
    database.dispose()


@pytest.fixture(autouse=True)
def route_service_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(
        app.dependency_overrides,
        get_video_processing_service,
        lambda: SimpleNamespace(get_job=lambda _: None),
    )
    monkeypatch.setitem(app.dependency_overrides, get_ai_service, lambda: SimpleNamespace())
    monkeypatch.setitem(app.dependency_overrides, get_media_service, MediaService)
    monkeypatch.setitem(app.dependency_overrides, get_blob_store, lambda: FakeBlobStore(tmp_path / "data"))


class FakeMedia:
    def extract_audio(self, media_path: Path, output_dir: Path) -> Path:
        audio_path = output_dir / "audio.wav"
        audio_path.write_bytes(b"audio")
        return audio_path


class FakeASR:
    def transcribe(self, audio_path: Path) -> list[ASRSegment]:
        return [ASRSegment(" first sentence ", 0.25, 1.75), ASRSegment("second sentence", 2.0, 3.5)]


def test_processing_preserves_timestamps_and_keeps_source(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"source")
    service = VideoProcessingService(
        FakeMedia(),
        FakeTranscriber([ASRSegment(" first sentence ", 0.25, 1.75), ASRSegment("second sentence", 2.0, 3.5)]),
        None,
        FakeBlobStore(tmp_path / "data"),
        FakeJobQueue(),
    )
    video_id = service.create_job()

    try:
        service.process(video_id, source)
        assert service.get_job(video_id).status == "completed"
        assert service.get_transcript(video_id) == [
            {"text": " first sentence ", "start": 0.25, "end": 1.75},
            {"text": "second sentence", "start": 2.0, "end": 3.5},
        ]
        assert source.exists()
    finally:
        (tmp_path / "data" / "transcripts" / f"{video_id}.json").unlink(missing_ok=True)


def test_processing_emits_stage_events(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"source")
    service = VideoProcessingService(FakeMedia(), FakeTranscriber(), None, FakeBlobStore(tmp_path / "data"), FakeJobQueue())
    video_id = service.create_job()

    try:
        service.process(video_id, source)
        events = service.get_events(video_id)
        assert any(event["event"] == "stage_started" and event["stage"] == "transcribing" for event in events)
        assert any(event["event"] == "stage_completed" and event["stage"] == "transcribing" for event in events)
        assert any(
            event["event"] == "stage_completed" and isinstance(event.get("duration_ms"), int)
            for event in events
        )
        assert events[-1]["event"] == "processing_completed"
    finally:
        (tmp_path / "data" / "transcripts" / f"{video_id}.json").unlink(missing_ok=True)


def test_processing_records_failure_and_keeps_source(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"source")
    service = VideoProcessingService(FakeMedia(), lambda: None, None, FakeBlobStore(tmp_path / "data"), FakeJobQueue())  # type: ignore[arg-type]
    video_id = service.create_job()

    try:
        service.process(video_id, source)
        job = service.get_job(video_id)
        assert job.status == "failed"
        assert job.error
        assert source.exists()
    finally:
        (tmp_path / "data" / "transcripts" / f"{video_id}.json").unlink(missing_ok=True)


def test_processing_resumes_from_checkpoint_and_skips_existing_outputs(tmp_path: Path) -> None:
    class CountingMedia(FakeMedia):
        def __init__(self) -> None:
            self.extractions = 0

        def extract_audio(self, media_path: Path, output_dir: Path) -> Path:
            self.extractions += 1
            return super().extract_audio(media_path, output_dir)

    media = CountingMedia()
    blob_store = FakeBlobStore(tmp_path / "data")
    queue = FakeJobQueue()
    service = VideoProcessingService(media, FakeTranscriber(), None, blob_store, queue)
    video_id = service.create_job()
    queued = queue.enqueue(video_id)
    claimed = queue.claim_job(queued.id, "worker")
    assert claimed is not None
    blob_store.put(f"audio/extracted/{video_id}.wav", b"existing audio")
    blob_store.write_transcript(
        video_id,
        [{"text": "already transcribed", "start": 2.0, "end": 3.0}],
    )
    assert queue.checkpoint(claimed.id, "worker", "extracting_audio")

    service.process_claimed(queue.get_job(video_id), "worker")

    result = service.get_job(video_id)
    assert result is not None and result.status == "completed"
    assert media.extractions == 0
    assert service.get_transcript(video_id) == [
        {"text": "already transcribed", "start": 2.0, "end": 3.0}
    ]
    assert any(event["event"] == "processing_completed" for event in service.get_events(video_id))


def test_processing_resumes_at_embedding_and_reuses_saved_chunks_and_summary(tmp_path: Path) -> None:
    class CountingAI:
        def __init__(self) -> None:
            self.prepare_calls = 0
            self.embed_calls = 0
            self.indexed_ids: list[str] = []
            self.summary_calls = 0
            self.audio_calls = 0

        def prepare_transcript_chunks(self, _video_id):
            self.prepare_calls += 1
            raise AssertionError("A completed chunk stage must not run again")

        def embed_transcript_chunks(self, _chunks, _progress=None):
            self.embed_calls += 1
            raise AssertionError("A completed embedding stage must not run again")

        def index_transcript_chunks(self, _video_id, chunks, embeddings, _progress=None):
            self.indexed_ids.extend(str(chunk["id"]) for chunk in chunks)
            assert len(embeddings) == len(chunks)

        def generate_summary(self, _chunks):
            self.summary_calls += 1
            raise AssertionError("An existing summary must be reused")

        def save_summary(self, _video_id, _summary):
            raise AssertionError("An existing summary must not be rewritten")

        def generate_summary_audio(self, _video_id, _summary):
            self.audio_calls += 1
            raise AssertionError("Existing summary audio must be reused")

    video_id = "c" * 32
    blob_store = FakeBlobStore(tmp_path / "data")
    queue = FakeJobQueue()
    ai = CountingAI()
    service = VideoProcessingService(FakeMedia(), FakeTranscriber(), ai, blob_store, queue)
    queued = queue.enqueue(video_id)
    claimed = queue.claim_job(queued.id, "worker")
    assert claimed is not None
    chunks = [{"id": "0", "text": "saved chunk", "metadata": {"video_id": video_id}}]
    blob_store.put(f"transcripts/{video_id}.chunks.json", json.dumps({"chunks": chunks}))
    blob_store.put(f"transcripts/{video_id}.embeddings.json", json.dumps({"embeddings": [[1.0, 0.0]]}))
    blob_store.put(f"summaries/{video_id}.json", json.dumps({"summary": {"topic": "saved"}}))
    blob_store.put(f"audio/summaries/{video_id}.wav", b"saved audio")
    assert queue.checkpoint(claimed.id, "worker", "embedding")

    service.process_claimed(queue.get_job(video_id), "worker")

    result = service.get_job(video_id)
    assert result is not None and result.status == "completed"
    assert ai.prepare_calls == ai.embed_calls == ai.summary_calls == ai.audio_calls == 0
    assert ai.indexed_ids == ["0"]


def test_youtube_processing_persists_source_in_existing_video_layout(tmp_path: Path) -> None:
    class YoutubeMedia(FakeMedia):
        def download_youtube(self, source_url: str, output_dir: Path, video_id: str) -> Path:
            assert source_url == "https://youtube.com/watch?v=example"
            source = output_dir / f"{video_id}.mp4"
            source.write_bytes(b"downloaded video")
            return source

    blob_store = FakeBlobStore(tmp_path / "data")
    service = VideoProcessingService(YoutubeMedia(), FakeTranscriber(), None, blob_store, FakeJobQueue())
    video_id = service.create_job()

    service.process(video_id, Path(), "https://youtube.com/watch?v=example")

    assert service.get_job(video_id).status == "completed"
    assert blob_store.open(f"videos/{video_id}.mp4").read_bytes() == b"downloaded video"


@pytest.mark.asyncio
async def test_upload_rejects_unsupported_media() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/videos/upload", files={"file": ("notes.txt", b"text", "text/plain")})
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_upload_rejects_bad_magic_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    authenticated_video_user: VideoOwnershipStore,
) -> None:
    video_id = "1" * 32
    monkeypatch.setitem(
        app.dependency_overrides,
        get_video_processing_service,
        lambda: SimpleNamespace(create_job=lambda: video_id),
    )
    blob_store = FakeBlobStore(tmp_path / "magic-data")
    monkeypatch.setitem(app.dependency_overrides, get_blob_store, lambda: blob_store)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/videos/upload",
            files={"file": ("source.mp4", b"not a video", "video/mp4")},
        )

    assert response.status_code == 400
    assert not blob_store.open(f"videos/{video_id}.mp4").exists()


@pytest.mark.asyncio
async def test_get_video_returns_404_for_unknown_video(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(app.dependency_overrides, get_video_processing_service, lambda: SimpleNamespace(get_job=lambda video_id: None))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/videos/unknown-video")

    assert response.status_code == 404
    assert response.json()["detail"] == "Video not found"


@pytest.mark.asyncio
async def test_get_video_returns_job_for_known_video(monkeypatch: pytest.MonkeyPatch, authenticated_video_user: VideoOwnershipStore) -> None:
    video_id = "a" * 32
    authenticated_video_user.register(video_id, "alice")
    monkeypatch.setitem(
        app.dependency_overrides,
        get_video_processing_service,
        lambda: SimpleNamespace(get_job=lambda requested_id: SimpleNamespace(video_id=requested_id, status="transcribing")),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/videos/{video_id}")

    assert response.status_code == 200
    assert response.json() == {"video_id": video_id, "status": "transcribing"}


@pytest.mark.asyncio
async def test_question_is_rejected_until_processing_completes(monkeypatch: pytest.MonkeyPatch, authenticated_video_user: VideoOwnershipStore) -> None:
    video_id = "b" * 32
    authenticated_video_user.register(video_id, "alice")
    class Job:
        status = "transcribing"

    class AI:
        def answer_question(self, video_id: str, question: str):
            raise AssertionError("LLM must not be called before processing completes")

    monkeypatch.setitem(app.dependency_overrides, get_video_processing_service, lambda: type("Service", (), {"get_job": lambda self, _: Job()})())
    monkeypatch.setitem(app.dependency_overrides, get_ai_service, AI)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/videos/{video_id}/question", json={"question": "What happened?"})

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_standalone_video_question_still_works(monkeypatch: pytest.MonkeyPatch, authenticated_video_user: VideoOwnershipStore) -> None:
    video_id = "c" * 32
    authenticated_video_user.register(video_id, "alice")
    class AI:
        def answer_question(self, video_id: str, question: str):
            assert (video_id, question) == ("c" * 32, "What happened?")
            return {"answer": "A standalone answer", "sources": []}

    monkeypatch.setitem(app.dependency_overrides, get_video_processing_service, lambda: SimpleNamespace(get_job=lambda video_id: SimpleNamespace(status="completed")))
    monkeypatch.setitem(app.dependency_overrides, get_ai_service, AI)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/videos/{video_id}/question", json={"question": "What happened?"})

    assert response.status_code == 200
    assert response.json() == {"answer": "A standalone answer", "sources": []}


def test_upload_rejects_huge_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(media_module, "settings", SimpleNamespace(max_upload_bytes=3))
    upload = SimpleNamespace(file=BytesIO(b"1234"))

    with pytest.raises(Exception, match="maximum allowed size"):
        MediaService().save_upload(upload, tmp_path / "too-large.mp4")


def test_missing_ffmpeg_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def missing_ffmpeg(*args, **kwargs):
        raise OSError("not found")

    monkeypatch.setattr(media_module.subprocess, "run", missing_ffmpeg)

    with pytest.raises(Exception, match="FFmpeg is not installed"):
        MediaService().extract_audio(tmp_path / "broken.mp4", tmp_path / "audio")


def test_corrupt_media_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        media_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stderr="invalid data"),
    )

    with pytest.raises(Exception, match="Audio extraction failed: invalid data"):
        MediaService().extract_audio(tmp_path / "broken.mp4", tmp_path / "audio")


@pytest.mark.asyncio
async def test_invalid_youtube_url_and_malformed_question_are_rejected(monkeypatch: pytest.MonkeyPatch, authenticated_video_user: VideoOwnershipStore) -> None:
    video_id = "d" * 32
    authenticated_video_user.register(video_id, "alice")
    monkeypatch.setitem(app.dependency_overrides, get_video_processing_service, lambda: SimpleNamespace(get_job=lambda requested_id: SimpleNamespace(status="completed")))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        invalid_url = await client.post("/videos/youtube", json={"url": "not-a-url"})
        malformed_question = await client.post(f"/videos/{video_id}/question", json={"question": 12})

    assert invalid_url.status_code == 422
    assert malformed_question.status_code == 422


def test_inaccessible_youtube_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        media_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stderr="private video", stdout=""
        ),
    )

    with pytest.raises(Exception, match="YouTube operation failed: private video"):
        MediaService().download_youtube("https://youtube.com/watch?v=private", tmp_path)


def test_concurrent_jobs_keep_independent_state(tmp_path: Path) -> None:
    class ConcurrentMedia(FakeMedia):
        pass

    service = VideoProcessingService(
        ConcurrentMedia(), FakeTranscriber(), None, FakeBlobStore(tmp_path / "data"), FakeJobQueue()
    )
    sources = [tmp_path / f"source-{index}.wav" for index in range(2)]
    video_ids = [service.create_job() for _ in sources]
    for source in sources:
        source.write_bytes(b"source")

    import threading

    threads = [threading.Thread(target=service.process, args=(video_id, source)) for video_id, source in zip(video_ids, sources)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert [service.get_job(video_id).status for video_id in video_ids] == ["completed", "completed"]


def test_youtube_downloader_persists_source_under_video_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        (tmp_path / "abc123.mp4").write_bytes(b"\x00\x00\x00\x18ftypisom")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(media_module.subprocess, "run", fake_run)
    monkeypatch.setattr(MediaService, "get_duration", lambda self, path: 30.0)
    monkeypatch.setattr(MediaService, "validate_magic", lambda self, path: None)
    result = MediaService().download_youtube("https://www.youtube.com/watch?v=example", tmp_path, "abc123")

    assert result == tmp_path / "abc123.mp4"
    assert calls[0][-1] == "https://www.youtube.com/watch?v=example"
    assert "--no-playlist" in calls[0]
    assert result.exists()


def test_youtube_request_rejects_non_youtube_hosts() -> None:
    with pytest.raises(Exception, match="Only YouTube URLs are supported"):
        YouTubeRequest(url="https://example.com/video")


def test_video_ownership_persists_and_cannot_be_reassigned(tmp_path: Path) -> None:
    video_id = "e" * 32
    database = create_database(tmp_path)
    original = VideoOwnershipStore(VideoRepo(database.session_factory))
    original.register(video_id, "alice")

    restored = VideoOwnershipStore(VideoRepo(database.session_factory))
    assert restored.is_owned_by(video_id, "alice")
    assert not restored.is_owned_by(video_id, "bob")
    with pytest.raises(Exception, match="Video ownership is unavailable"):
        restored.register(video_id, "bob")
    database.dispose()


@pytest.mark.asyncio
async def test_video_upload_and_youtube_jobs_are_persistently_owned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, authenticated_video_user: VideoOwnershipStore
) -> None:
    video_ids = iter(["f" * 32, "0" * 32])
    jobs = SimpleNamespace(create_job=lambda: next(video_ids))
    started: list[tuple[str, str | None]] = []

    class UploadMedia:
        @staticmethod
        def validate_filename(filename: str) -> str:
            return ".mp4"

        @staticmethod
        def save_upload_with_digest(file, destination: Path) -> SimpleNamespace:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(file.file.read())
            return SimpleNamespace(sha256="a" * 64, size=5)

        @staticmethod
        def get_duration(_path: Path) -> float:
            return 60.0

        @staticmethod
        def get_youtube_duration(_url: str) -> float:
            return 120.0

    monkeypatch.setitem(app.dependency_overrides, get_video_processing_service, lambda: jobs)
    monkeypatch.setitem(app.dependency_overrides, get_media_service, UploadMedia)
    monkeypatch.setitem(app.dependency_overrides, get_blob_store, lambda: FakeBlobStore(tmp_path / "data"))
    monkeypatch.setattr(videos_api, "_start_processing", lambda _service, video_id: started.append((video_id, None)))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        uploaded = await client.post("/videos/upload", files={"file": ("source.mp4", b"video", "video/mp4")})
        youtube = await client.post("/videos/youtube", json={"url": "https://www.youtube.com/watch?v=example"})

    assert uploaded.status_code == youtube.status_code == 202
    assert authenticated_video_user.is_owned_by("f" * 32, "alice")
    assert authenticated_video_user.is_owned_by("0" * 32, "alice")
    assert started == [("f" * 32, None), ("0" * 32, None)]


@pytest.mark.asyncio
async def test_video_routes_reject_anonymous_and_foreign_users(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    authenticated_video_user: VideoOwnershipStore,
) -> None:
    video_id = "9" * 32
    authenticated_video_user.register(video_id, "alice")
    media_path = tmp_path / "data" / "videos" / f"{video_id}.mp4"
    media_path.parent.mkdir(parents=True, exist_ok=True)
    media_path.write_bytes(b"private video")
    monkeypatch.setitem(app.dependency_overrides, get_video_processing_service, lambda: SimpleNamespace(get_job=lambda _: SimpleNamespace(status="completed")))
    monkeypatch.setitem(app.dependency_overrides, get_blob_store, lambda: FakeBlobStore(tmp_path / "data"))

    app.dependency_overrides.pop(get_current_user)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anonymous:
        anonymous_response = await anonymous.get(f"/videos/{video_id}/media")
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser("bob")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as bob:
        status_response = await bob.get(f"/videos/{video_id}/status")
        media_response = await bob.get(f"/videos/{video_id}/media")
        question_response = await bob.post(f"/videos/{video_id}/question", json={"question": "What happened?"})

    assert anonymous_response.status_code == 401
    assert status_response.status_code == media_response.status_code == question_response.status_code == 404


@pytest.mark.asyncio
async def test_events_endpoint_replays_only_events_after_last_event_id(
    monkeypatch: pytest.MonkeyPatch,
    authenticated_video_user: VideoOwnershipStore,
) -> None:
    video_id = "d" * 32
    authenticated_video_user.register(video_id, "alice")
    events = [
        {
            "id": event_id,
            "event": "stage_completed",
            "video_id": video_id,
            "stage": "validating",
            "status": "completed",
            "progress": 100,
            "message": "done",
            "detail": None,
            "timestamp": "2026-10-30T12:00:00+00:00",
        }
        for event_id in (7, 8, 9)
    ]
    service = SimpleNamespace(
        get_job=lambda _: SimpleNamespace(status="completed"),
        get_events=lambda _, cursor=0: [event.copy() for event in events if event["id"] > cursor],
    )
    monkeypatch.setitem(app.dependency_overrides, get_video_processing_service, lambda: service)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/videos/{video_id}/events",
            headers={"Last-Event-ID": "7"},
        )

    assert response.status_code == 200
    assert "id: 8\n" in response.text
    assert "id: 9\n" in response.text
    assert "id: 7\n" not in response.text


@pytest.mark.asyncio
async def test_media_endpoint_requires_completed_processing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, authenticated_video_user: VideoOwnershipStore) -> None:
    media_id = "a" * 32
    authenticated_video_user.register(media_id, "alice")
    media_path = tmp_path / "data" / "videos" / f"{media_id}.mp4"
    media_path.parent.mkdir(parents=True, exist_ok=True)
    media_path.write_bytes(b"video")
    monkeypatch.setitem(app.dependency_overrides, get_video_processing_service, lambda: SimpleNamespace(get_job=lambda video_id: SimpleNamespace(status="transcribing")))
    monkeypatch.setitem(app.dependency_overrides, get_blob_store, lambda: FakeBlobStore(tmp_path / "data"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/videos/{media_id}/media")

    assert response.status_code == 409
    assert "not ready" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_media_endpoint_serves_persisted_video(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, authenticated_video_user: VideoOwnershipStore) -> None:
    media_id = "b" * 32
    authenticated_video_user.register(media_id, "alice")
    media_path = tmp_path / "data" / "videos" / f"{media_id}.mp4"
    media_path.parent.mkdir(parents=True, exist_ok=True)
    media_path.write_bytes(b"video")
    monkeypatch.setitem(app.dependency_overrides, get_video_processing_service, lambda: SimpleNamespace(get_job=lambda video_id: SimpleNamespace(status="completed")))
    monkeypatch.setitem(app.dependency_overrides, get_blob_store, lambda: FakeBlobStore(tmp_path / "data"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/videos/{media_id}/media")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("video/mp4")
    assert response.content == b"video"


@pytest.mark.asyncio
async def test_media_endpoint_rejects_unknown_or_unsafe_ids(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(app.dependency_overrides, get_video_processing_service, lambda: SimpleNamespace(get_job=lambda video_id: SimpleNamespace(status="completed")))
    monkeypatch.setitem(app.dependency_overrides, get_blob_store, lambda: FakeBlobStore(tmp_path / "data"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.get("/videos/" + "c" * 32 + "/media")
        unsafe = await client.get("/videos/../../etc/passwd/media")

    assert missing.status_code == 404
    assert unsafe.status_code in {404, 422}


@pytest.mark.asyncio
async def test_audio_artifact_rejects_unsafe_identifiers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        app.dependency_overrides,
        get_video_processing_service,
        lambda: SimpleNamespace(get_job=lambda video_id: SimpleNamespace(status="completed")),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/videos/../../answers/../../audio")

    assert response.status_code in {404, 422}