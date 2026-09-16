import json
import logging
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from backend.app.core.config import settings
from backend.app.services.asr import ASRService, FasterWhisperASRService
from backend.app.services.media import MediaProcessingError, MediaService, temporary_directory
from backend.app.services.llm import VideoAIService

logger = logging.getLogger(__name__)


@dataclass
class VideoJob:
    video_id: str
    status: str
    updated_at: datetime
    error: str | None = None


class VideoProcessingService:
    def __init__(self, media: MediaService | None = None, asr: ASRService | None = None, ai: VideoAIService | None = None) -> None:
        self.media = media or MediaService()
        self.asr = asr
        self.ai = ai
        self._jobs: dict[str, VideoJob] = {}
        self._lock = threading.Lock()

    def create_job(self) -> str:
        video_id = uuid.uuid4().hex
        self._set_job(video_id, "queued")
        return video_id

    def get_job(self, video_id: str) -> VideoJob | None:
        with self._lock:
            return self._jobs.get(video_id)

    def process(self, video_id: str, media_path: Path, source_url: str | None = None) -> None:
        try:
            with temporary_directory() as temp_dir:
                if source_url:
                    self._set_job(video_id, "downloading")
                    media_path = self.media.download_youtube(source_url, Path(temp_dir))
                self._set_job(video_id, "extracting_audio")
                audio_path = self.media.extract_audio(media_path, Path(temp_dir))
                self._set_job(video_id, "transcribing")
                asr = self.asr or FasterWhisperASRService()
                segments = asr.transcribe(audio_path)
                if not segments:
                    raise MediaProcessingError("Transcription produced no speech segments")
                self._write_transcript(video_id, segments)
                if self.ai is not None:
                    self._set_job(video_id, "indexing")
                    self.ai.index_and_summarize(video_id)
            self._set_job(video_id, "completed")
        except Exception as exc:
            logger.exception("Video processing failed for %s", video_id)
            self._set_job(video_id, "failed", str(exc))
        finally:
            if not source_url:
                media_path.unlink(missing_ok=True)

    def get_transcript(self, video_id: str) -> list[dict[str, object]] | None:
        path = settings.project_root / "data" / "transcripts" / f"{video_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))["segments"]

    def _write_transcript(self, video_id: str, segments) -> None:
        path = settings.project_root / "data" / "transcripts" / f"{video_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"video_id": video_id, "segments": [asdict(segment) for segment in segments]}, indent=2), encoding="utf-8")

    def _set_job(self, video_id: str, status: str, error: str | None = None) -> None:
        with self._lock:
            self._jobs[video_id] = VideoJob(video_id, status, datetime.now(timezone.utc), error)