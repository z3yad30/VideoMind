import sys
import types
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from backend.app.main import app
from backend.app.services.asr import ASRSegment
from backend.app.services.media import MediaService
from backend.app.services.video_processing import VideoProcessingService


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