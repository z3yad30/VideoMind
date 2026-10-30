import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from backend.app.ports import BlobStore, JobQueue, JobRecord, Segment, Transcriber
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
    def __init__(
        self,
        media: MediaService,
        transcriber: Transcriber,
        ai: VideoAIService | None,
        blob_store: BlobStore,
        job_queue: JobQueue,
    ) -> None:
        self.media = media
        self.transcriber = transcriber
        self.ai = ai
        self.blob_store = blob_store
        self.job_queue = job_queue

    def create_job(self) -> str:
        return uuid.uuid4().hex

    def enqueue(self, video_id: str) -> JobRecord:
        return self.job_queue.enqueue(video_id)

    def get_job(self, video_id: str) -> VideoJob | None:
        job = self.job_queue.get_job(video_id)
        if job is None:
            return None
        stages = [self._stage_record(stage_id, name) for stage_id, name in STAGES]
        stage_lookup = {str(stage["id"]): stage for stage in stages}
        for event in self.job_queue.events_since(job.id, 0):
            payload = event.payload_json
            stage_id = event.stage
            stage = stage_lookup.get(stage_id or "")
            if stage is None:
                continue
            stage.update(
                status=payload.get("status", stage["status"]),
                progress=payload.get("progress"),
                message=payload.get("message"),
                detail=payload.get("detail"),
                error=payload.get("detail") if payload.get("status") == "failed" else None,
            )
            if payload.get("status") == "running":
                stage["started_at"] = payload.get("timestamp")
            elif payload.get("status") in {"completed", "failed", "skipped"}:
                stage["completed_at"] = payload.get("timestamp")

        if job.state in {"queued", "completed", "failed"}:
            status = job.state
        else:
            active = next((str(stage["id"]) for stage in stages if stage["status"] == "running"), None)
            status = active or job.stage or "validating"
        return VideoJob(job.video_id, status, job.updated_at, job.error, stages)

    def get_events(self, video_id: str, last_event_id: int = 0) -> list[dict[str, object]]:
        job = self.job_queue.get_job(video_id)
        if job is None:
            return []
        return [
            {"id": event.id, **event.payload_json}
            for event in self.job_queue.events_since(job.id, last_event_id)
        ]

    def process(self, video_id: str, media_path: Path, source_url: str | None = None) -> None:
        job = self.job_queue.enqueue(video_id)
        claimed = self.job_queue.claim_job(job.id, "inline-worker")
        if claimed is None:
            raise RuntimeError(f"Unable to claim newly enqueued video job {video_id}")
        self.process_claimed(claimed, "inline-worker", media_path, source_url)

    def process_claimed(
        self,
        job: JobRecord,
        worker_id: str,
        media_path: Path | None = None,
        source_url: str | None = None,
    ) -> None:
        video_id = job.video_id
        media_path = media_path or self._find_source(video_id, required=False)
        source_url = source_url or job.source_url
        active_stage = "validating"
        try:
            checkpoint_index = self._stage_index(job.stage)

            def execute_stage(
                stage_id: str,
                message: str,
                action: Callable[[], None] | None = None,
                output_key: str | None = None,
                skip_message: str | None = None,
            ) -> None:
                nonlocal active_stage
                active_stage = stage_id
                if self._stage_index(stage_id) <= checkpoint_index:
                    return
                if output_key and self.blob_store.exists(output_key):
                    self._stage(
                        video_id,
                        stage_id,
                        "completed",
                        f"{message} (already available)",
                        worker_id=worker_id,
                        job_id=job.id,
                    )
                    return
                if skip_message is not None:
                    self._stage(video_id, stage_id, "skipped", skip_message, worker_id=worker_id, job_id=job.id)
                    return
                self._stage(video_id, stage_id, "running", message, worker_id=worker_id, job_id=job.id)
                if action is not None:
                    action()
                self._stage(video_id, stage_id, "completed", message, worker_id=worker_id, job_id=job.id)

            execute_stage(
                "validating",
                "Media source accepted",
                lambda: self._validate_source(video_id, media_path, source_url),
            )

            if source_url:
                def download() -> None:
                    nonlocal media_path
                    existing = self._find_source(video_id, required=False)
                    if existing is not None:
                        media_path = existing
                        return
                    with temporary_directory() as temp_dir:
                        downloaded = self.media.download_youtube(source_url, Path(temp_dir), video_id)
                        media_path = self.blob_store.put(f"videos/{video_id}{downloaded.suffix}", downloaded)

                execute_stage("downloading", "Video downloaded", download)
            else:
                execute_stage("downloading", "Video downloaded", skip_message="Not needed for an uploaded file")

            def extract_audio() -> None:
                nonlocal media_path
                if media_path is None:
                    source_path = self._find_source(video_id, required=True)
                    if source_path is None:
                        raise MediaProcessingError("Video source file is unavailable")
                    media_path = source_path
                key = f"audio/extracted/{video_id}.wav"
                with temporary_directory() as temp_dir:
                    extracted_path = self.media.extract_audio(media_path, Path(temp_dir))
                    self.blob_store.put(key, extracted_path)

            audio_key = f"audio/extracted/{video_id}.wav"
            execute_stage("extracting_audio", "Audio extracted", extract_audio, output_key=audio_key)
            execute_stage(
                "detecting_language",
                "Language detection is provided by transcription",
                skip_message="Language detection is provided by transcription",
            )

            transcript_key = f"transcripts/{video_id}.json"

            def transcribe() -> None:
                if not self.blob_store.exists(audio_key):
                    raise MediaProcessingError("Extracted audio is unavailable")
                segments = self.transcriber.transcribe(self.blob_store.open(audio_key))
                if not segments:
                    raise MediaProcessingError("Transcription produced no speech segments")
                self._write_transcript(video_id, segments)

            execute_stage("transcribing", "Transcription completed", transcribe, output_key=transcript_key)
            execute_stage(
                "building_transcript",
                "Transcript ready",
                output_key=transcript_key,
            )

            if self.ai is None:
                for stage_id, _ in STAGES[6:]:
                    execute_stage(stage_id, "Optional enrichment is not configured", skip_message="Optional enrichment is not configured")
            else:
                chunks_key = f"transcripts/{video_id}.chunks.json"
                chunks: list[dict[str, object]] = []

                def build_chunks() -> None:
                    chunks.extend(self.ai.prepare_transcript_chunks(video_id))
                    self.blob_store.put(chunks_key, json.dumps({"chunks": chunks}))
                    self._emit_progress(video_id, "chunking", len(chunks), len(chunks), job.id)

                execute_stage("chunking", "Transcript chunks built", build_chunks, output_key=chunks_key)
                if not chunks and self.blob_store.exists(chunks_key):
                    stored_chunks = self._read_json(chunks_key).get("chunks")
                    if not isinstance(stored_chunks, list) or not all(isinstance(chunk, dict) for chunk in stored_chunks):
                        raise MediaProcessingError("Invalid transcript chunks checkpoint")
                    chunks = stored_chunks

                embeddings_key = f"transcripts/{video_id}.embeddings.json"
                embeddings: list[list[float]] = []

                def embed_chunks() -> None:
                    embeddings.extend(self.ai.embed_transcript_chunks(chunks, self._rag_progress(video_id, job.id)))
                    self.blob_store.put(embeddings_key, json.dumps({"embeddings": embeddings}))

                execute_stage("embedding", "Embeddings generated", embed_chunks, output_key=embeddings_key)
                if not embeddings and self.blob_store.exists(embeddings_key):
                    stored_embeddings = self._read_json(embeddings_key).get("embeddings")
                    if (
                        not isinstance(stored_embeddings, list)
                        or not all(
                            isinstance(vector, list)
                            and all(isinstance(value, (int, float)) for value in vector)
                            for vector in stored_embeddings
                        )
                    ):
                        raise MediaProcessingError("Invalid transcript embeddings checkpoint")
                    embeddings = [[float(value) for value in vector] for vector in stored_embeddings]

                def index_chunks() -> None:
                    self.ai.index_transcript_chunks(
                        video_id,
                        chunks,
                        embeddings,
                        self._rag_progress(video_id, job.id),
                    )

                execute_stage("indexing", "Knowledge indexed", index_chunks)

                summary_key = f"summaries/{video_id}.json"
                summary: dict[str, str] = {}

                def summarize() -> None:
                    summary.update(self.ai.generate_summary(chunks))
                    self.ai.save_summary(video_id, summary)

                execute_stage("summarizing", "Summary generated", summarize, output_key=summary_key)
                if not summary and self.blob_store.exists(summary_key):
                    loaded = self._read_json(summary_key).get("summary")
                    if not isinstance(loaded, dict) or not all(
                        isinstance(key, str) and isinstance(value, str) for key, value in loaded.items()
                    ):
                        raise MediaProcessingError("Invalid summary checkpoint")
                    summary = {str(key): str(value) for key, value in loaded.items()}

                audio_summary_key = f"audio/summaries/{video_id}.wav"
                execute_stage(
                    "generating_summary_audio",
                    "Voice summary ready",
                    lambda: self.ai.generate_summary_audio(video_id, summary),
                    output_key=audio_summary_key,
                )

            self.job_queue.record_event(
                job.id,
                "processing_completed",
                None,
                {
                    **self._event_payload(video_id, None, "completed", 100, "Video ready"),
                    "event": "processing_completed",
                },
            )
            if not self.job_queue.complete(job.id, worker_id):
                raise RuntimeError("Worker no longer owns this video job")
        except Exception as exc:
            logger.exception("Video processing failed for %s", video_id)
            self._stage(video_id, active_stage, "failed", "Processing failed", str(exc), worker_id, job.id)
            if not self.job_queue.fail(job.id, worker_id, str(exc)):
                logger.warning("Could not mark lost-owned job %s as failed", job.id)

    def get_transcript(self, video_id: str) -> list[dict[str, object]] | None:
        key = f"transcripts/{video_id}.json"
        if not self.blob_store.exists(key):
            return None
        return json.loads(self.blob_store.open(key).read_text(encoding="utf-8"))["segments"]

    def _validate_source(self, video_id: str, media_path: Path | None, source_url: str | None) -> None:
        if source_url:
            return
        source = media_path or self._find_source(video_id, required=True)
        if hasattr(self.media, "validate_filename"):
            self.media.validate_filename(source.name)

    def _find_source(self, video_id: str, required: bool) -> Path | None:
        media_dir = self.blob_store.open(f"videos/{video_id}").parent
        matches = sorted(
            path
            for path in media_dir.glob(f"{video_id}.*")
            if path.is_file()
            and path.suffix.lower() in getattr(self.media, "supported_extensions", MediaService.supported_extensions)
        )
        if matches:
            return matches[0]
        if required:
            raise MediaProcessingError("Video source file is unavailable")
        return None

    def _write_transcript(self, video_id: str, segments: list[Segment]) -> None:
        self.blob_store.put(
            f"transcripts/{video_id}.json",
            json.dumps(
                {
                    "video_id": video_id,
                    "segments": [
                        {"text": segment.text, "start": segment.start, "end": segment.end}
                        for segment in segments
                    ],
                },
                indent=2,
            ),
        )

    def _read_json(self, key: str) -> dict[str, object]:
        try:
            value = json.loads(self.blob_store.open(key).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MediaProcessingError(f"Unable to read processing checkpoint: {key}") from exc
        if not isinstance(value, dict):
            raise MediaProcessingError(f"Invalid processing checkpoint: {key}")
        return value

    def _stage(
        self,
        video_id: str,
        stage_id: str,
        status: str,
        message: str,
        detail: str | None = None,
        worker_id: str | None = None,
        job_id: str | None = None,
    ) -> None:
        event_type = "stage_failed" if status == "failed" else (
            "stage_started" if status == "running" else "stage_completed"
        )
        payload = self._event_payload(
            video_id,
            stage_id,
            status,
            100 if status in {"completed", "skipped"} else None,
            message,
            detail,
        )
        payload["event"] = event_type
        if worker_id and job_id:
            if status in {"completed", "skipped"}:
                if not self.job_queue.record_checkpoint(job_id, worker_id, stage_id, event_type, payload):
                    raise RuntimeError("Worker no longer owns this video job")
                return
            if not self.job_queue.heartbeat(job_id, worker_id):
                raise RuntimeError("Worker no longer owns this video job")
        self.job_queue.record_event(
            job_id or self._job_id(video_id),
            event_type,
            stage_id,
            payload,
        )

    def _job_id(self, video_id: str) -> str:
        job = self.job_queue.get_job(video_id)
        if job is None:
            raise RuntimeError(f"Video job does not exist: {video_id}")
        return job.id

    @staticmethod
    def _event_payload(
        video_id: str,
        stage: str | None,
        status: str,
        progress: int | None,
        message: str,
        detail: str | None = None,
    ) -> dict[str, object]:
        return {
            "event": None,
            "video_id": video_id,
            "stage": stage,
            "status": status,
            "progress": progress,
            "message": message,
            "detail": detail,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def _emit_progress(self, video_id: str, stage: str, current: int, total: int, job_id: str) -> None:
        progress = round(current / total * 100) if total else None
        self.job_queue.record_event(
            job_id,
            "stage_progress",
            stage,
            {
                **self._event_payload(
                    video_id,
                    stage,
                    "running",
                    progress,
                    dict(STAGES)[stage],
                    f"{current} / {total} chunks" if total else "Preparing chunks",
                ),
                "event": "stage_progress",
            },
        )

    def _rag_progress(self, video_id: str, job_id: str) -> Callable[[str, int, int], None]:
        def report(stage: str, current: int, total: int) -> None:
            self._emit_progress(video_id, stage, current, total, job_id)

        return report

    @staticmethod
    def _stage_index(stage: str | None) -> int:
        return next((index for index, (stage_id, _) in enumerate(STAGES) if stage_id == stage), -1)

    @staticmethod
    def _stage_record(stage_id: str, name: str) -> dict[str, object]:
        return {
            "id": stage_id,
            "display_name": name,
            "status": "pending",
            "progress": None,
            "message": None,
            "detail": None,
            "started_at": None,
            "completed_at": None,
            "error": None,
        }
