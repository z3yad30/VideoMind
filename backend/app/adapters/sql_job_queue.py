from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import case, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session as OrmSession

from backend.app.db.models import Job, JobEvent, MediaAsset, Video
from backend.app.ports import JobEventRecord, JobRecord


class JobQueueError(RuntimeError):
    pass


class SqlJobQueue:
    def __init__(self, session_factory: Callable[[], OrmSession]) -> None:
        self.session_factory = session_factory

    def enqueue(self, video_id: str) -> JobRecord:
        try:
            with self.session_factory.begin() as session:
                job = session.scalar(select(Job).where(Job.video_id == video_id))
                if job is None:
                    job = Job(video_id=video_id, state="queued", attempts=0)
                    session.add(job)
                    session.flush()
                return self._record(job, session.get(Video, video_id))
        except IntegrityError as exc:
            existing = self.get_job(video_id)
            if existing is not None:
                return existing
            raise JobQueueError("Unable to enqueue video job") from exc
        except SQLAlchemyError as exc:
            raise JobQueueError("Unable to enqueue video job") from exc

    def claim_next(self, worker_id: str) -> JobRecord | None:
        try:
            with self.session_factory.begin() as session:
                candidate_id = (
                    select(Job.id)
                    .where(Job.state == "queued")
                    .order_by(Job.created_at, Job.id)
                    .limit(1)
                    .scalar_subquery()
                )
                return self._claim(session, candidate_id, worker_id)
        except SQLAlchemyError as exc:
            raise JobQueueError("Unable to claim video job") from exc

    def claim_job(self, job_id: str, worker_id: str) -> JobRecord | None:
        try:
            with self.session_factory.begin() as session:
                return self._claim(session, job_id, worker_id)
        except SQLAlchemyError as exc:
            raise JobQueueError("Unable to claim video job") from exc

    def heartbeat(self, job_id: str, worker_id: str) -> bool:
        now = datetime.now(timezone.utc)
        return self._update_owned(job_id, worker_id, {"locked_at": now, "updated_at": now})

    def checkpoint(self, job_id: str, worker_id: str, stage: str) -> bool:
        now = datetime.now(timezone.utc)
        return self._update_owned(job_id, worker_id, {"stage": stage, "locked_at": now, "updated_at": now})

    def record_checkpoint(
        self,
        job_id: str,
        worker_id: str,
        stage: str,
        event_type: str,
        payload: dict[str, object],
    ) -> bool:
        now = datetime.now(timezone.utc)
        try:
            with self.session_factory.begin() as session:
                updated = session.execute(
                    update(Job)
                    .where(Job.id == job_id, Job.state == "running", Job.locked_by == worker_id)
                    .values(stage=stage, locked_at=now, updated_at=now)
                )
                if updated.rowcount != 1:
                    return False
                session.add(JobEvent(job_id=job_id, type=event_type, stage=stage, payload_json=payload))
                return True
        except SQLAlchemyError as exc:
            raise JobQueueError("Unable to checkpoint video job") from exc

    def complete(self, job_id: str, worker_id: str) -> bool:
        now = datetime.now(timezone.utc)
        try:
            with self.session_factory.begin() as session:
                job = session.scalar(
                    select(Job).where(
                        Job.id == job_id,
                        Job.state == "running",
                        Job.locked_by == worker_id,
                    )
                )
                if job is None:
                    return False
                video = session.get(Video, job.video_id)
                job.state = "completed"
                job.locked_by = None
                job.locked_at = None
                job.updated_at = now
                if video and video.asset_id:
                    session.execute(
                        update(MediaAsset).where(MediaAsset.id == video.asset_id).values(status="completed")
                    )
                    session.execute(update(Video).where(Video.asset_id == video.asset_id).values(status="completed"))
                elif video:
                    video.status = "completed"
                return True
        except SQLAlchemyError as exc:
            raise JobQueueError("Unable to complete video job") from exc

    def fail(self, job_id: str, worker_id: str, error: str) -> bool:
        now = datetime.now(timezone.utc)
        try:
            with self.session_factory.begin() as session:
                job = session.scalar(
                    select(Job).where(
                        Job.id == job_id,
                        Job.state == "running",
                        Job.locked_by == worker_id,
                    )
                )
                if job is None:
                    return False
                video = session.get(Video, job.video_id)
                job.state = "failed"
                job.error = error
                job.locked_by = None
                job.locked_at = None
                job.updated_at = now
                if video and video.asset_id:
                    session.execute(
                        update(MediaAsset).where(MediaAsset.id == video.asset_id).values(status="failed")
                    )
                    session.execute(update(Video).where(Video.asset_id == video.asset_id).values(status="failed"))
                elif video:
                    video.status = "failed"
                return True
        except SQLAlchemyError as exc:
            raise JobQueueError("Unable to fail video job") from exc

    def mark_completed(self, video_id: str) -> JobRecord:
        now = datetime.now(timezone.utc)
        try:
            with self.session_factory.begin() as session:
                job = session.scalar(select(Job).where(Job.video_id == video_id))
                if job is None:
                    job = Job(
                        video_id=video_id,
                        state="completed",
                        stage="generating_summary_audio",
                        attempts=0,
                    )
                    session.add(job)
                    session.flush()
                video = session.get(Video, video_id)
                if video is None:
                    raise JobQueueError("Cannot complete a job for a missing video")
                video.status = "completed"
                if video.asset_id:
                    session.execute(
                        update(MediaAsset).where(MediaAsset.id == video.asset_id).values(status="completed")
                    )
                    session.execute(update(Video).where(Video.asset_id == video.asset_id).values(status="completed"))
                job.state = "completed"
                job.stage = "generating_summary_audio"
                job.updated_at = now
                session.add(
                    JobEvent(
                        job_id=job.id,
                        type="processing_completed",
                        stage=None,
                        payload_json={
                            "event": "processing_completed",
                            "video_id": video_id,
                            "stage": None,
                            "status": "completed",
                            "progress": 100,
                            "message": "Existing processed media reused",
                            "detail": None,
                            "timestamp": now.isoformat(),
                        },
                    )
                )
                session.flush()
                return self._record(job, video)
        except SQLAlchemyError as exc:
            raise JobQueueError("Unable to mark deduplicated video complete") from exc

    def record_event(
        self, job_id: str, event_type: str, stage: str | None, payload: dict[str, object]
    ) -> int:
        try:
            with self.session_factory.begin() as session:
                event = JobEvent(job_id=job_id, type=event_type, stage=stage, payload_json=payload)
                session.add(event)
                session.flush()
                return event.id
        except SQLAlchemyError as exc:
            raise JobQueueError("Unable to record job event") from exc

    def events_since(self, job_id: str, last_event_id: int) -> list[JobEventRecord]:
        try:
            with self.session_factory() as session:
                events = session.scalars(
                    select(JobEvent)
                    .where(JobEvent.job_id == job_id, JobEvent.id > last_event_id)
                    .order_by(JobEvent.id)
                ).all()
                return [
                    JobEventRecord(event.id, event.job_id, event.type, event.stage, event.payload_json, event.created_at)
                    for event in events
                ]
        except SQLAlchemyError as exc:
            raise JobQueueError("Unable to read job events") from exc

    def get_job(self, video_id: str) -> JobRecord | None:
        try:
            with self.session_factory() as session:
                video = session.get(Video, video_id)
                if video is None:
                    return None
                if video.asset_id:
                    job = session.scalar(
                        select(Job)
                        .join(Video, Job.video_id == Video.id)
                        .where(
                            Video.asset_id == video.asset_id,
                            Job.state.in_(("queued", "running", "completed", "failed")),
                        )
                        .order_by(
                            case(
                                (Job.state == "running", 0),
                                (Job.state == "queued", 1),
                                (Job.state == "completed", 2),
                                else_=3,
                            ),
                            Job.updated_at.desc(),
                            Job.id,
                        )
                        .limit(1)
                    )
                else:
                    job = session.scalar(select(Job).where(Job.video_id == video_id))
                if job is None:
                    return None
                return self._record(job, video)
        except SQLAlchemyError as exc:
            raise JobQueueError("Unable to read video job") from exc

    def recover_stale(self, stale_after_seconds: int) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)
        try:
            with self.session_factory.begin() as session:
                recovered = session.execute(
                    update(Job)
                    .where(Job.state == "running", Job.locked_at < cutoff)
                    .values(
                        state="queued",
                        locked_by=None,
                        locked_at=None,
                        updated_at=datetime.now(timezone.utc),
                    )
                    .returning(Job.id, Job.video_id, Job.stage)
                ).all()
                for job_id, video_id, stage in recovered:
                    session.add(
                        JobEvent(
                            job_id=job_id,
                            type="processing_resumed",
                            stage=stage,
                            payload_json={
                                "event": "processing_resumed",
                                "video_id": video_id,
                                "stage": stage,
                                "status": "queued",
                                "message": "Recovering job after worker heartbeat expired",
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            },
                        )
                    )
                return len(recovered)
        except SQLAlchemyError as exc:
            raise JobQueueError("Unable to recover stale jobs") from exc

    def _update_owned(self, job_id: str, worker_id: str, values: dict[str, object]) -> bool:
        try:
            with self.session_factory.begin() as session:
                result = session.execute(
                    update(Job)
                    .where(Job.id == job_id, Job.state == "running", Job.locked_by == worker_id)
                    .values(**values)
                )
                return result.rowcount == 1
        except SQLAlchemyError as exc:
            raise JobQueueError("Unable to update video job") from exc

    @staticmethod
    def _claim(session: OrmSession, job_id, worker_id: str) -> JobRecord | None:
        now = datetime.now(timezone.utc)
        result = session.execute(
            update(Job)
            .where(Job.id == job_id, Job.state == "queued")
            .values(
                state="running",
                attempts=Job.attempts + 1,
                locked_by=worker_id,
                locked_at=now,
                updated_at=now,
                error=None,
            )
            .returning(Job)
        ).scalar_one_or_none()
        if result is None:
            return None
        session.refresh(result)
        video = session.get(Video, result.video_id)
        if video and video.asset_id:
            session.execute(
                update(MediaAsset).where(MediaAsset.id == video.asset_id).values(status="running")
            )
            session.execute(update(Video).where(Video.asset_id == video.asset_id).values(status="running"))
        elif video:
            video.status = "running"
        return SqlJobQueue._record(result, video)

    @staticmethod
    def _record(job: Job, video: Video | None) -> JobRecord:
        if video is None:
            raise JobQueueError("Video job has no associated video")
        return JobRecord(
            id=job.id,
            video_id=job.video_id,
            state=job.state,
            stage=job.stage,
            attempts=job.attempts,
            error=job.error,
            locked_by=job.locked_by,
            locked_at=job.locked_at,
            created_at=job.created_at,
            updated_at=job.updated_at,
            source_type=video.source_type,
            source_url=video.source_url,
            asset_id=video.asset_id,
            blob_key=video.asset.blob_key if video.asset is not None else None,
            user_id=video.user_id,
        )
