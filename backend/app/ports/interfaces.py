from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Segment:
    text: str
    start: float
    end: float


class Transcriber(Protocol):
    def transcribe(self, audio_path: Path) -> list[Segment]: ...


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class VectorStore(Protocol):
    def upsert(self, video_id: str, chunks: list[dict[str, object]]) -> None: ...

    def query(self, video_id: str, embedding: list[float], top_k: int) -> list[dict[str, object]]: ...

    def exists(self, video_id: str) -> bool: ...

    def delete_video(self, video_id: str) -> None: ...


class LLMClient(Protocol):
    def complete(self, messages: list[dict[str, str]], **opts: object) -> str: ...


class TTS(Protocol):
    def synthesize(self, text: str, out_path: Path) -> Path: ...


class BlobStore(Protocol):
    def put(self, key: str, source: Path | bytes | str) -> Path: ...

    def open(self, key: str) -> Path: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...

    def delete_prefix(self, prefix: str) -> None: ...


@dataclass(frozen=True)
class JobRecord:
    id: str
    video_id: str
    state: str
    stage: str | None
    attempts: int
    error: str | None
    locked_by: str | None
    locked_at: datetime | None
    created_at: datetime
    updated_at: datetime
    source_type: str
    source_url: str | None
    asset_id: str | None = None
    blob_key: str | None = None
    user_id: str | None = None


@dataclass(frozen=True)
class JobEventRecord:
    id: int
    job_id: str
    type: str
    stage: str | None
    payload_json: dict[str, object]
    created_at: datetime


class JobQueue(Protocol):
    def enqueue(self, video_id: str) -> JobRecord: ...

    def claim_next(self, worker_id: str) -> JobRecord | None: ...

    def claim_job(self, job_id: str, worker_id: str) -> JobRecord | None: ...

    def heartbeat(self, job_id: str, worker_id: str) -> bool: ...

    def checkpoint(self, job_id: str, worker_id: str, stage: str) -> bool: ...

    def record_checkpoint(
        self,
        job_id: str,
        worker_id: str,
        stage: str,
        event_type: str,
        payload: dict[str, object],
    ) -> bool: ...

    def complete(self, job_id: str, worker_id: str) -> bool: ...

    def mark_completed(self, video_id: str) -> JobRecord: ...

    def fail(self, job_id: str, worker_id: str, error: str) -> bool: ...

    def record_event(
        self, job_id: str, event_type: str, stage: str | None, payload: dict[str, object]
    ) -> int: ...

    def events_since(self, job_id: str, last_event_id: int) -> list[JobEventRecord]: ...

    def get_job(self, video_id: str) -> JobRecord | None: ...

    def recover_stale(self, stale_after_seconds: int) -> int: ...
