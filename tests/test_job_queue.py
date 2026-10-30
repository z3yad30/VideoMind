from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event

from sqlalchemy import update

from backend.app.adapters.sql_job_queue import SqlJobQueue
from backend.app.db.models import Job
from backend.app.repositories.video import VideoRepo
from backend.worker import WorkerRunner
from tests.database import create_database, seed_user
from tests.fakes import FakeJobQueue


def create_queued_job(tmp_path, video_id: str = "a" * 32):
    database = create_database(tmp_path)
    seed_user(database, "alice")
    VideoRepo(database.session_factory).register(video_id, "alice")
    queue = SqlJobQueue(database.session_factory)
    job = queue.enqueue(video_id)
    return database, queue, job


def test_two_workers_cannot_claim_the_same_job(tmp_path) -> None:
    database, queue, job = create_queued_job(tmp_path)
    barrier = Barrier(2)

    def claim(worker_id: str):
        barrier.wait()
        return queue.claim_next(worker_id)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, ["worker-a", "worker-b"]))
        claimed = [result for result in results if result is not None]
        assert len(claimed) == 1
        assert claimed[0].id == job.id
        assert claimed[0].locked_by in {"worker-a", "worker-b"}
        assert queue.claim_next("third-worker") is None
    finally:
        database.dispose()


def test_stale_running_job_is_requeued_with_checkpoint_and_attempt_count(tmp_path) -> None:
    database, queue, created = create_queued_job(tmp_path)
    first_claim = queue.claim_next("worker-a")
    assert first_claim is not None
    assert queue.record_checkpoint(
        first_claim.id,
        "worker-a",
        "transcribing",
        "stage_completed",
        {"event": "stage_completed", "stage": "transcribing", "status": "completed"},
    )
    with database.session_factory.begin() as session:
        session.execute(
            update(Job)
            .where(Job.id == created.id)
            .values(locked_at=datetime.now(timezone.utc) - timedelta(minutes=5))
        )

    try:
        assert queue.recover_stale(30) == 1
        recovered = queue.get_job(created.video_id)
        assert recovered is not None
        assert recovered.state == "queued"
        assert recovered.stage == "transcribing"
        assert recovered.locked_by is None
        assert [event.type for event in queue.events_since(created.id, 0)] == ["stage_completed", "processing_resumed"]
        second_claim = queue.claim_next("worker-b")
        assert second_claim is not None
        assert second_claim.stage == "transcribing"
        assert second_claim.attempts == 2
        assert second_claim.locked_by == "worker-b"
    finally:
        database.dispose()


def test_events_since_replays_only_ids_after_cursor(tmp_path) -> None:
    database, queue, job = create_queued_job(tmp_path)
    first = queue.record_event(job.id, "stage_started", "validating", {"event": "stage_started"})
    second = queue.record_event(job.id, "stage_completed", "validating", {"event": "stage_completed"})
    third = queue.record_event(job.id, "stage_started", "extracting_audio", {"event": "stage_started"})

    try:
        replayed = queue.events_since(job.id, first)
        assert [event.id for event in replayed] == [second, third]
    finally:
        database.dispose()


def test_shared_asset_status_prefers_the_current_retry_job(tmp_path) -> None:
    database = create_database(tmp_path)
    seed_user(database, "alice")
    seed_user(database, "bob")
    seed_user(database, "carol")
    repository = VideoRepo(database.session_factory)
    first = repository.register_upload("a" * 32, "alice", "d" * 64, 10, 5.0, ".mp4")
    repository.register_upload("b" * 32, "bob", "d" * 64, 10, 5.0, ".mp4")
    repository.register_upload("c" * 32, "carol", "d" * 64, 10, 5.0, ".mp4")
    queue = SqlJobQueue(database.session_factory)
    original = queue.enqueue("a" * 32)
    original_claim = queue.claim_next("worker-a")
    assert original_claim is not None
    assert queue.fail(original.id, "worker-a", "transient failure")
    retry = queue.enqueue("b" * 32)
    retry_claim = queue.claim_next("worker-b")
    assert retry_claim is not None and retry_claim.id == retry.id

    try:
        assert first.asset_id is not None
        assert repository.asset_id_for_video("c" * 32, "carol") == first.asset_id
        shared = queue.get_job("c" * 32)
        retried = queue.get_job("a" * 32)
        assert shared is not None and shared.state == "running"
        assert retried is not None and retried.video_id == "b" * 32
    finally:
        database.dispose()


def test_worker_loop_claims_and_processes_a_queued_job() -> None:
    queue = FakeJobQueue()
    job = queue.enqueue("video")
    finished = Event()

    class Processor:
        def process_claimed(self, claimed, worker_id: str) -> None:
            assert claimed.id == job.id
            assert queue.complete(claimed.id, worker_id)
            finished.set()

    runner = WorkerRunner(queue, Processor(), max_concurrent_jobs=1, stale_after_seconds=3, poll_interval_seconds=0.01)
    runner.start()
    try:
        assert finished.wait(2)
    finally:
        runner.stop()
    final = queue.get_job(job.video_id)
    assert final is not None and final.state == "completed"
    assert final.attempts == 1
