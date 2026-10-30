from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import wave

import pytest
from httpx import ASGITransport, AsyncClient

from backend.app.main import app
from backend.app.api.dependencies import get_current_user
from backend.app.container import get_video_processing_service, get_voice_service
from backend.app.repositories.video import VideoRepo
from backend.app.services.auth import AuthenticatedUser
from backend.app.services.asr import ASRSegment
from backend.app.services.llm import SUMMARY_FIELDS, VideoAIService
from backend.app.services.media import MediaService
from backend.app.services.voice import VoiceQuestionService
import backend.app.api.videos as videos_api
from backend.app.services.video_storage import VideoOwnershipStore
from tests.fakes import FakeBlobStore, FakeTTS
from tests.database import create_database, seed_user


def _wav_bytes() -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 320)
    return output.getvalue()


class FakeASR:
    def transcribe(self, audio_path: Path) -> list[ASRSegment]:
        assert audio_path.exists()
        return [ASRSegment("What happened next?", 0, 1)]


class FakeAI:
    def __init__(self) -> None:
        self.questions: list[tuple[str, str]] = []

    def answer_question(self, video_id: str, question: str) -> dict[str, object]:
        self.questions.append((video_id, question))
        return {
            "answer": "The next step was deployment.",
            "sources": [{"start": 2.0, "end": 3.0, "text": "Deployment followed."}],
        }

    def index_and_summarize(self, video_id: str) -> None:
        raise AssertionError("voice questions must not index anything")


@pytest.mark.asyncio
async def test_voice_question_runs_asr_rag_and_tts_without_indexing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    video_id = "a" * 32
    database = create_database(tmp_path)
    seed_user(database, "alice")
    ownership = VideoOwnershipStore(VideoRepo(database.session_factory))
    ownership.register(video_id, "alice")
    monkeypatch.setattr(videos_api, "video_ownership", ownership)
    monkeypatch.setitem(app.dependency_overrides, get_current_user, lambda: AuthenticatedUser("alice"))
    ai = FakeAI()
    tts = FakeTTS()
    service = VoiceQuestionService(
        ai=ai, media=MediaService(), transcriber=FakeASR(), tts=tts, blob_store=FakeBlobStore(tmp_path / "data")
    )
    monkeypatch.setitem(app.dependency_overrides, get_voice_service, lambda: service)
    monkeypatch.setitem(
        app.dependency_overrides,
        get_video_processing_service,
        lambda: SimpleNamespace(get_job=lambda video_id: SimpleNamespace(status="completed")),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/videos/{video_id}/voice-question",
            files={"file": ("question.wav", _wav_bytes(), "audio/wav")},
        )

    assert response.status_code == 200
    assert response.json()["transcribed_question"] == "What happened next?"
    assert response.json()["answer"] == "The next step was deployment."
    assert response.json()["sources"] == [{"start": 2.0, "end": 3.0, "text": "Deployment followed."}]
    assert response.json()["audio_answer_location"].startswith(f"/videos/{video_id}/answers/")
    assert ai.questions == [(video_id, "What happened next?")]
    assert len(tts.calls) == 1
    database.dispose()


def test_summary_is_synthesized_with_replaceable_tts(tmp_path: Path) -> None:
    summary = {field: field for field in SUMMARY_FIELDS}

    class FakeLLM:
        def summarize_hierarchically(self, texts: list[str]) -> dict[str, str]:
            return summary

    class FakeRAG:
        def index_transcript(self, video_id: str, on_progress=None) -> list[dict[str, object]]:
            return [{"text": "transcript"}]

    tts = FakeTTS()
    service = VideoAIService(
        llm=FakeLLM(), rag=FakeRAG(), blob_store=FakeBlobStore(tmp_path / "data"), tts=tts
    )

    service.index_and_summarize("video-1")

    assert service.get_summary_audio_path("video-1") == tmp_path / "data" / "audio" / "summaries" / "video-1.wav"
    assert tts.calls[0][0].startswith("Overview: Overview")


def test_tts_failure_is_normalized(tmp_path: Path) -> None:
    class BrokenTTS:
        def synthesize(self, text: str, output_path: Path) -> Path:
            raise OSError("audio device unavailable")

    service = VoiceQuestionService(
        ai=FakeAI(), media=MediaService(), transcriber=FakeASR(), tts=BrokenTTS(),
        blob_store=FakeBlobStore(tmp_path / "data")
    )

    with pytest.raises(RuntimeError, match="TTS synthesis failed"):
        service.answer("video-1", SimpleNamespace(filename="question.wav", file=BytesIO(_wav_bytes())))