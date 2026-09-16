import uuid
from pathlib import Path

from backend.app.core.config import settings
from backend.app.services.asr import ASRService, FasterWhisperASRService
from backend.app.services.llm import VideoAIService
from backend.app.services.media import MediaService, temporary_directory
from backend.app.services.tts import Pyttsx3TTSService, TTSService


class VoiceQuestionService:
    def __init__(
        self,
        ai: VideoAIService,
        media: MediaService | None = None,
        asr: ASRService | None = None,
        tts: TTSService | None = None,
        audio_dir: Path | None = None,
    ) -> None:
        self.ai = ai
        self.media = media or MediaService()
        self.asr = asr
        self.tts = tts
        self.audio_dir = audio_dir or settings.project_root / "data" / "audio" / "answers"

    def answer(self, video_id: str, upload) -> dict[str, object]:
        suffix = self.media.validate_filename(upload.filename)
        with temporary_directory() as temp_dir:
            question_path = Path(temp_dir) / f"question{suffix}"
            self.media.save_upload(upload, question_path)
            asr = self.asr or FasterWhisperASRService()
            segments = asr.transcribe(question_path)

        question = " ".join(segment.text.strip() for segment in segments if segment.text.strip()).strip()
        if not question:
            raise ValueError("Voice question produced no speech")

        result = self.ai.answer_question(video_id, question)
        answer_id = uuid.uuid4().hex
        audio_path = self.audio_dir / video_id / f"{answer_id}.wav"
        try:
            (self.tts or Pyttsx3TTSService()).synthesize(str(result["answer"]), audio_path)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("TTS synthesis failed") from exc
        return {
            "transcribed_question": question,
            "answer": result["answer"],
            "sources": result["sources"],
            "answer_id": answer_id,
            "audio_path": audio_path,
        }