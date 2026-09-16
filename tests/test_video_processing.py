import sys
import types
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from backend.app.main import app
from backend.app.services.asr import ASRSegment
from backend.app.services.media import MediaService
from backend.app.services.video_processing import VideoProcessingService
from backend.app.schemas.videos import YouTubeRequest
import backend.app.api.videos as videos_api
import backend.app.services.media as media_module


class FakeMedia:
    def extract_audio(self, media_path: Path, output_dir: Path) -> Path:
        audio_path = output_dir / "audio.wav"
        audio_path.write_bytes(b"audio")
        return audio_path


class FakeASR:
    def transcribe(self, audio_path: Path) -> list[ASRSegment]:
        return [ASRSegment(" first sentence ", 0.25, 1.75), ASRSegment("second sentence", 2.0, 3.5)]


def test_processing_preserves_timestamps_and_cleans_source() -> None:
    source = Path("data/videos/test-phase2-source.wav")
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"source")
    service = VideoProcessingService(media=FakeMedia(), asr=FakeASR())
    video_id = service.create_job()

    try:
        service.process(video_id, source)
        assert service.get_job(video_id).status == "completed"
        assert service.get_transcript(video_id) == [
            {"text": " first sentence ", "start": 0.25, "end": 1.75},
            {"text": "second sentence", "start": 2.0, "end": 3.5},
        ]
        assert not source.exists()
    finally:
        (Path("data/transcripts") / f"{video_id}.json").unlink(missing_ok=True)


def test_processing_records_failure_and_cleans_source() -> None:
    source = Path("data/videos/test-phase2-failure.wav")
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"source")
    service = VideoProcessingService(media=FakeMedia(), asr=lambda: None)  # type: ignore[arg-type]
    video_id = service.create_job()

    try:
        service.process(video_id, source)
        job = service.get_job(video_id)
        assert job.status == "failed"
        assert job.error
        assert not source.exists()
    finally:
        (Path("data/transcripts") / f"{video_id}.json").unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_upload_rejects_unsupported_media() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/videos/upload", files={"file": ("notes.txt", b"text", "text/plain")})
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_question_is_rejected_until_processing_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    class Job:
        status = "transcribing"

    class AI:
        def answer_question(self, video_id: str, question: str):
            raise AssertionError("LLM must not be called before processing completes")

    monkeypatch.setattr(videos_api, "video_service", type("Service", (), {"get_job": lambda self, _: Job()})())
    monkeypatch.setattr(videos_api, "ai_service", AI())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/videos/video-1/question", json={"question": "What happened?"})

    assert response.status_code == 409


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
async def test_invalid_youtube_url_and_malformed_question_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        videos_api,
        "video_service",
        SimpleNamespace(get_job=lambda video_id: SimpleNamespace(status="completed")),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        invalid_url = await client.post("/videos/youtube", json={"url": "not-a-url"})
        malformed_question = await client.post("/videos/video-1/question", json={"question": 12})

    assert invalid_url.status_code == 422
    assert malformed_question.status_code == 422


def test_inaccessible_youtube_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class FailingDownloader:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def download(self, urls):
            raise RuntimeError("private video")

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FailingDownloader))

    with pytest.raises(Exception, match="YouTube download failed: private video"):
        MediaService().download_youtube("https://youtube.com/watch?v=private", tmp_path)


def test_concurrent_jobs_keep_independent_state(tmp_path: Path) -> None:
    class ConcurrentMedia(FakeMedia):
        pass

    service = VideoProcessingService(media=ConcurrentMedia(), asr=FakeASR())
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


def test_youtube_downloader_uses_single_video_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict]] = []

    class FakeDownloader:
        def __init__(self, options):
            calls.append(([], options))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def download(self, urls):
            calls[-1] = (urls, calls[-1][1])
            (tmp_path / "source.mp4").write_bytes(b"video")

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeDownloader))
    result = MediaService().download_youtube("https://www.youtube.com/watch?v=example", tmp_path)

    assert result == tmp_path / "source.mp4"
    assert calls[0][0] == ["https://www.youtube.com/watch?v=example"]
    assert calls[0][1]["noplaylist"] is True


def test_youtube_request_rejects_non_youtube_hosts() -> None:
    with pytest.raises(Exception, match="Only YouTube URLs are supported"):
        YouTubeRequest(url="https://example.com/video")


@pytest.mark.asyncio
async def test_audio_artifact_rejects_unsafe_identifiers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        videos_api,
        "video_service",
        SimpleNamespace(get_job=lambda video_id: SimpleNamespace(status="completed")),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/videos/../../answers/../../audio")

    assert response.status_code in {404, 422}