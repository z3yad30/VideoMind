from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from backend.app.main import app
from backend.app.services.asr import ASRSegment
from backend.app.services.llm import SUMMARY_FIELDS, VideoAIService
from backend.app.services.media import MediaService
from backend.app.services.voice import VoiceQuestionService
import backend.app.api.videos as videos_api


class FakeASR:
    def transcribe(self, audio_path: Path) -> list[ASRSegment]:
        assert audio_path.exists()
        return [ASRSegment("What happened next?", 0, 1)]


class FakeTTS:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Path]] = []

    def synthesize(self, text: str, output_path: Path) -> Path:
        self.calls.append((text, output_path))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"wav")
        return output_path


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
    ai = FakeAI()
    tts = FakeTTS()
    service = VoiceQuestionService(
        ai=ai, media=MediaService(), asr=FakeASR(), tts=tts, audio_dir=tmp_path / "answers"
    )
    monkeypatch.setattr(videos_api, "voice_service", service)
    monkeypatch.setattr(videos_api, "video_service", SimpleNamespace(get_job=lambda video_id: object()))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/videos/video-1/voice-question",
            files={"file": ("question.wav", b"microphone audio", "audio/wav")},
        )

    assert response.status_code == 200
    assert response.json()["transcribed_question"] == "What happened next?"
    assert response.json()["answer"] == "The next step was deployment."
    assert response.json()["sources"] == [{"start": 2.0, "end": 3.0, "text": "Deployment followed."}]
    assert response.json()["audio_answer_location"].startswith("/videos/video-1/answers/")
    assert ai.questions == [("video-1", "What happened next?")]
    assert len(tts.calls) == 1


def test_summary_is_synthesized_with_replaceable_tts(tmp_path: Path) -> None:
    summary = {field: field for field in SUMMARY_FIELDS}

    class FakeLLM:
        def summarize_hierarchically(self, texts: list[str]) -> dict[str, str]:
            return summary

    class FakeRAG:
        def index_transcript(self, video_id: str) -> list[dict[str, object]]:
            return [{"text": "transcript"}]

    tts = FakeTTS()
    service = VideoAIService(
        llm=FakeLLM(), rag=FakeRAG(), summary_dir=tmp_path / "summaries", tts=tts, audio_dir=tmp_path / "audio"
    )

    service.index_and_summarize("video-1")

    assert service.get_summary_audio_path("video-1") == tmp_path / "audio" / "video-1.wav"
    assert tts.calls[0][0].startswith("Overview: Overview")