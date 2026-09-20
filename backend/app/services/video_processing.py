import json
import logging
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

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
    stages: list[dict[str, object]] | None = None


STAGES = [
    ("validating", "Validate media"),
    ("downloading", "Download source"),
    ("extracting_audio", "Extract audio"),
    ("detecting_language", "Detect language"),
    ("transcribing", "Transcribe video"),
    ("building_transcript", "Build transcript"),
    ("chunking", "Build transcript chunks"),
    ("embedding", "Generate embeddings"),
    ("indexing", "Index knowledge"),
    ("summarizing", "Generate summary"),
    ("generating_summary_audio", "Generate voice summary"),
]


class VideoProcessingService:
    def __init__(self, media: MediaService | None = None, asr: ASRService | None = None, ai: VideoAIService | None = None) -> None:
        self.media = media or MediaService()
        self.asr = asr
        self.ai = ai
        self._asr_lock = threading.Lock()
        self._jobs: dict[str, VideoJob] = {}
        self._events: dict[str, list[dict[str, object]]] = {}
        self._lock = threading.Lock()

    def create_job(self) -> str:
        video_id = uuid.uuid4().hex
        self._set_job(video_id, "queued")
        return video_id

    def get_job(self, video_id: str) -> VideoJob | None:
        with self._lock:
            job = self._jobs.get(video_id)
            if job and job.stages is None:
                job.stages = [self._stage_record(stage_id, name) for stage_id, name in STAGES]
            return job

    def get_events(self, video_id: str) -> list[dict[str, object]]:
        with self._lock:
            return list(self._events.get(video_id, []))

    def process(self, video_id: str, media_path: Path, source_url: str | None = None) -> None:
        try:
            with temporary_directory() as temp_dir:
                self._stage(video_id, "validating", "running", "Checking media source")
                if not source_url and hasattr(self.media, "validate_filename"):
                    self.media.validate_filename(media_path.name)
                self._stage(video_id, "validating", "completed", "Media source accepted")
                if source_url:
                    self._stage(video_id, "downloading", "running", "Downloading YouTube video")
                    media_path = self.media.download_youtube(source_url, settings.project_root / "data" / "videos", video_id)
                    self._stage(video_id, "downloading", "completed", "Video downloaded")
                else:
                    self._stage(video_id, "downloading", "skipped", "Not needed for an uploaded file")
                self._stage(video_id, "extracting_audio", "running", "Extracting audio")
                audio_path = self.media.extract_audio(media_path, Path(temp_dir))
                self._stage(video_id, "extracting_audio", "completed", "Audio extracted")
                self._stage(video_id, "detecting_language", "skipped", "Language detection is provided by transcription")
                self._stage(video_id, "transcribing", "running", "Transcribing audio", detail="Processing audio segments")
                asr = self._get_asr()
                segments = asr.transcribe(audio_path)
                if not segments:
                    raise MediaProcessingError("Transcription produced no speech segments")
                self._stage(video_id, "transcribing", "completed", "Transcription completed", detail=f"{len(segments)} segments transcribed")
                self._stage(video_id, "building_transcript", "running", "Building timestamped transcript")
                self._write_transcript(video_id, segments)
                self._stage(video_id, "building_transcript", "completed", "Transcript ready", detail=f"{len(segments)} segments")
                if self.ai is not None:
                    self._stage(video_id, "chunking", "running", "Building transcript chunks")
                    rag = self.ai._rag()
                    chunks = rag.index_transcript(video_id, on_progress=self._rag_progress(video_id))
                    self._stage(video_id, "chunking", "completed", "Transcript chunks built", detail=f"{len(chunks)} chunks")
                    self._stage(video_id, "embedding", "completed", "Embeddings generated", detail=f"{len(chunks)} chunks")
                    self._stage(video_id, "indexing", "completed", "Knowledge indexed", detail="Video-specific knowledge base")
                    self._stage(video_id, "summarizing", "running", "Analyzing transcript and extracting the main ideas")
                    summary = self.ai.generate_summary(chunks)
                    self.ai.save_summary(video_id, summary)
                    self._stage(video_id, "summarizing", "completed", "Summary generated")
                    self._stage(video_id, "generating_summary_audio", "running", "Generating voice summary")
                    self.ai.generate_summary_audio(video_id, summary)
                    self._stage(video_id, "generating_summary_audio", "completed", "Voice summary ready")
                else:
                    for stage_id, _ in STAGES[6:]:
                        self._stage(video_id, stage_id, "skipped", "Optional enrichment is not configured")
            self._set_job(video_id, "completed")
            self._emit(video_id, "processing_completed", None, "completed", 100, "Video ready")
        except Exception as exc:
            logger.exception("Video processing failed for %s", video_id)
            current = self.get_job(video_id)
            failed_stage = next((stage["id"] for stage in (current.stages or []) if stage["status"] == "running"), "processing") if current else "processing"
            self._stage(video_id, str(failed_stage), "failed", "Processing failed", str(exc))
            self._set_job(video_id, "failed", str(exc))

    def _get_asr(self) -> ASRService:
        if self.asr is not None:
            return self.asr
        with self._asr_lock:
            if self.asr is None:
                logger.info("Loading ASR model '%s' on %s", settings.asr_model, settings.asr_device)
                self.asr = FasterWhisperASRService()
                logger.info("ASR model '%s' loaded", settings.asr_model)
            return self.asr

    def get_transcript(self, video_id: str) -> list[dict[str, object]] | None:
        path = settings.project_root / "data" / "transcripts" / f"{video_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))["segments"]

    def _write_transcript(self, video_id: str, segments) -> None:
        path = settings.project_root / "data" / "transcripts" / f"{video_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps({"video_id": video_id, "segments": [asdict(segment) for segment in segments]}, indent=2),
            encoding="utf-8",
        )
        temporary_path.replace(path)

    def _set_job(self, video_id: str, status: str, error: str | None = None) -> None:
        with self._lock:
            previous = self._jobs.get(video_id)
            stages = previous.stages if previous else [self._stage_record(stage_id, name) for stage_id, name in STAGES]
            self._jobs[video_id] = VideoJob(video_id, status, datetime.now(timezone.utc), error, stages)

    @staticmethod
    def _stage_record(stage_id: str, name: str) -> dict[str, object]:
        return {"id": stage_id, "display_name": name, "status": "pending", "progress": None, "message": None, "detail": None, "started_at": None, "completed_at": None, "error": None}

    def _stage(self, video_id: str, stage_id: str, status: str, message: str, detail: str | None = None) -> None:
        with self._lock:
            job = self._jobs[video_id]
            stage = next(item for item in job.stages or [] if item["id"] == stage_id)
            now = datetime.now(timezone.utc)
            stage.update(status=status, message=message, detail=detail, progress=100 if status in {"completed", "skipped"} else None)
            if status == "running": stage["started_at"] = now
            if status in {"completed", "failed", "skipped"}: stage["completed_at"] = now
        self._set_job(video_id, "transcribing" if stage_id == "transcribing" else stage_id, detail if status == "failed" else None)
        self._emit(video_id, "stage_failed" if status == "failed" else ("stage_started" if status == "running" else "stage_completed"), stage_id, status, stage["progress"], message, detail)

    def _emit(self, video_id: str, event: str, stage: str | None, status: str, progress: int | None, message: str, detail: str | None = None) -> None:
        payload = {"event": event, "video_id": video_id, "stage": stage, "status": status, "progress": progress, "message": message, "detail": detail, "timestamp": datetime.now(timezone.utc)}
        with self._lock:
            self._events.setdefault(video_id, []).append(payload)

    def _rag_progress(self, video_id: str) -> Callable[[str, int, int], None]:
        def report(stage: str, current: int, total: int) -> None:
            stage_id = {"chunking": "chunking", "embedding": "embedding", "indexing": "indexing"}[stage]
            progress = round(current / total * 100) if total else None
            self._emit(video_id, "stage_progress", stage_id, "running", progress, dict(STAGES)[stage_id], f"{current} / {total} chunks" if total else "Preparing chunks")
        return report