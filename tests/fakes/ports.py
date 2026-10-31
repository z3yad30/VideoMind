import json
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.app.ports import JobEventRecord, JobRecord, Segment


class FakeBlobStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    def open(self, key: str) -> Path:
        path = self.data_dir / key
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def put(self, key: str, source: Path | bytes | str) -> Path:
        path = self.open(key)
        if isinstance(source, Path):
            path.write_bytes(source.read_bytes())
        elif isinstance(source, bytes):
            path.write_bytes(source)
        else:
            path.write_text(source, encoding="utf-8")
        return path

    def exists(self, key: str) -> bool:
        return (self.data_dir / key).is_file()

    def delete(self, key: str) -> None:
        (self.data_dir / key).unlink(missing_ok=True)

    def delete_prefix(self, prefix: str) -> None:
        path = self.data_dir / prefix
        if path.is_dir():
            import shutil

            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    def write_transcript(self, video_id: str, segments: list[dict[str, object]]) -> None:
        self.put(f"transcripts/{video_id}.json", json.dumps({"video_id": video_id, "segments": segments}))


class FakeJobQueue:
    def __init__(self) -> None:
        self.jobs: dict[str, JobRecord] = {}
        self.events: list[JobEventRecord] = []
        self._lock = threading.Lock()

    def enqueue(self, video_id: str) -> JobRecord:
        with self._lock:
            for job in self.jobs.values():
                if job.video_id == video_id:
                    return job
            now = datetime.now(timezone.utc)
            job = JobRecord(uuid4().hex, video_id, "queued", None, 0, None, None, None, now, now, "upload", None)
            self.jobs[job.id] = job
            return job

    def claim_next(self, worker_id: str) -> JobRecord | None:
        with self._lock:
            job = next((item for item in self.jobs.values() if item.state == "queued"), None)
            return self._claim(job, worker_id) if job else None

    def claim_job(self, job_id: str, worker_id: str) -> JobRecord | None:
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None or job.state != "queued":
                return None
            return self._claim(job, worker_id)

    def heartbeat(self, job_id: str, worker_id: str) -> bool:
        return self._update_owned(job_id, worker_id, locked_at=datetime.now(timezone.utc))

    def checkpoint(self, job_id: str, worker_id: str, stage: str) -> bool:
        return self._update_owned(job_id, worker_id, stage=stage, locked_at=datetime.now(timezone.utc))

    def record_checkpoint(
        self,
        job_id: str,
        worker_id: str,
        stage: str,
        event_type: str,
        payload: dict[str, object],
    ) -> bool:
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None or job.state != "running" or job.locked_by != worker_id:
                return False
            self.jobs[job_id] = replace(
                job,
                stage=stage,
                locked_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            event_id = len(self.events) + 1
            self.events.append(
                JobEventRecord(event_id, job_id, event_type, stage, payload, datetime.now(timezone.utc))
            )
            return True

    def complete(self, job_id: str, worker_id: str) -> bool:
        return self._update_owned(job_id, worker_id, state="completed", locked_by=None, locked_at=None)

    def mark_completed(self, video_id: str) -> JobRecord:
        job = self.enqueue(video_id)
        completed = replace(job, state="completed")
        self.jobs[job.id] = completed
        return completed

    def fail(self, job_id: str, worker_id: str, error: str) -> bool:
        return self._update_owned(job_id, worker_id, state="failed", error=error, locked_by=None, locked_at=None)

    def record_event(self, job_id: str, event_type: str, stage: str | None, payload: dict[str, object]) -> int:
        with self._lock:
            event_id = len(self.events) + 1
            self.events.append(
                JobEventRecord(event_id, job_id, event_type, stage, payload, datetime.now(timezone.utc))
            )
            return event_id

    def events_since(self, job_id: str, last_event_id: int) -> list[JobEventRecord]:
        with self._lock:
            return [event for event in self.events if event.job_id == job_id and event.id > last_event_id]

    def get_job(self, video_id: str) -> JobRecord | None:
        with self._lock:
            return next((job for job in self.jobs.values() if job.video_id == video_id), None)

    def recover_stale(self, stale_after_seconds: int) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)
        recovered = 0
        with self._lock:
            for job_id, job in list(self.jobs.items()):
                if job.state == "running" and job.locked_at and job.locked_at < cutoff:
                    self.jobs[job_id] = replace(
                        job, state="queued", locked_by=None, locked_at=None, updated_at=datetime.now(timezone.utc)
                    )
                    recovered += 1
        return recovered

    def _claim(self, job: JobRecord, worker_id: str) -> JobRecord:
        now = datetime.now(timezone.utc)
        claimed = replace(
            job,
            state="running",
            attempts=job.attempts + 1,
            locked_by=worker_id,
            locked_at=now,
            updated_at=now,
        )
        self.jobs[job.id] = claimed
        return claimed

    def _update_owned(self, job_id: str, worker_id: str, **values: object) -> bool:
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None or job.state != "running" or job.locked_by != worker_id:
                return False
            self.jobs[job_id] = replace(job, updated_at=datetime.now(timezone.utc), **values)
            return True


class FakeTranscriber:
    def __init__(self, segments: list[Segment] | None = None) -> None:
        self.segments = segments or [Segment("first sentence", 0.25, 1.75), Segment("second sentence", 2.0, 3.5)]

    def transcribe(self, audio_path: Path) -> list[Segment]:
        return self.segments


class FakeEmbedder:
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(text)), float(text.lower().count("python"))] for text in texts]


class FakeVectorStore:
    def __init__(self, results: list[dict[str, object]] | None = None) -> None:
        self.results = results or []
        self.chunks: dict[str, list[dict[str, object]]] = {}
        self.queries: list[tuple[str, list[float], int]] = []
        self.hybrid_queries: list[tuple[str, list[float], str, int]] = []

    def upsert(self, video_id: str, chunks: list[dict[str, object]]) -> None:
        self.chunks[video_id] = chunks

    def query(self, video_id: str, embedding: list[float], top_k: int) -> list[dict[str, object]]:
        self.queries.append((video_id, embedding, top_k))
        return [chunk for chunk in self.results if chunk.get("metadata", {}).get("video_id", video_id) == video_id][:top_k]

    def query_hybrid(
        self,
        video_id: str,
        embedding: list[float],
        _query_text: str,
        top_k: int,
    ) -> list[dict[str, object]]:
        self.hybrid_queries.append((video_id, embedding, _query_text, top_k))
        return self.query(video_id, embedding, top_k)

    def exists(self, video_id: str) -> bool:
        return video_id in self.chunks

    def delete_video(self, video_id: str) -> None:
        self.chunks.pop(video_id, None)


class FakeReranker:
    def rerank(
        self,
        _query: str,
        candidates: list[dict[str, object]],
        top_k: int,
    ) -> list[dict[str, object]]:
        return list(reversed(candidates))[:top_k]


class FakeLLMClient:
    def __init__(self, responses: list[str] | None = None) -> None:
        self.responses = list(responses or [])
        self.prompts: list[str] = []
        self.messages: list[list[dict[str, str]]] = []
        self.options: list[dict[str, object]] = []

    def complete(self, messages: list[dict[str, str]], **opts: object) -> str:
        self.messages.append(messages)
        self.prompts.append("\n\n".join(message["content"] for message in messages))
        self.options.append(opts)
        return self.responses.pop(0)


class FakeTTS:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Path]] = []

    def synthesize(self, text: str, out_path: Path) -> Path:
        self.calls.append((text, out_path))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"wav")
        return out_path
