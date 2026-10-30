import uuid
from pathlib import Path

from backend.app.ports import BlobStore, TTS, Transcriber
from backend.app.services.llm import VideoAIService
from backend.app.services.media import MediaService, temporary_directory


class VoiceQuestionService:
    def __init__(
        self,
        ai: VideoAIService,
        media: MediaService,
        transcriber: Transcriber,
        tts: TTS,
        blob_store: BlobStore,
    ) -> None:
        self.ai = ai
        self.media = media
        self.transcriber = transcriber
        self.tts = tts
        self.blob_store = blob_store
        self.audio_dir = blob_store.open("audio/answers")

    def answer(self, video_id: str, upload) -> dict[str, object]:
        suffix = self.media.validate_filename(upload.filename)
        with temporary_directory() as temp_dir:
            question_path = Path(temp_dir) / f"question{suffix}"
            self.media.save_upload(upload, question_path)
            segments = self.transcriber.transcribe(question_path)

        question = " ".join(segment.text.strip() for segment in segments if segment.text.strip()).strip()
        if not question:
            raise ValueError("Voice question produced no speech")

        result = self.ai.answer_question(video_id, question)
        answer_id, audio_path = self.synthesize_answer(video_id, str(result["answer"]))
        return {
            "transcribed_question": question,
            "answer": result["answer"],
            "sources": result["sources"],
            "answer_id": answer_id,
            "audio_path": audio_path,
        }

    def synthesize_answer(self, video_id: str, answer: str) -> tuple[str, Path]:
        answer_id = uuid.uuid4().hex
        audio_path = self.blob_store.open(f"audio/answers/{video_id}/{answer_id}.wav")
        try:
            self.tts.synthesize(answer, audio_path)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("TTS synthesis failed") from exc
        return answer_id, audio_path
