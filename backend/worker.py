from concurrent.futures import Future, ThreadPoolExecutor
import logging
import os
import socket
import threading
from uuid import uuid4

from backend.app.core.config import settings
from backend.app.ports import JobQueue, JobRecord
from backend.app.services.video_processing import VideoProcessingService

logger = logging.getLogger(__name__)
STALE_AFTER_SECONDS = 60


class WorkerRunner:
    def __init__(
        self,
        job_queue: JobQueue,
        video_processing: VideoProcessingService,
        max_concurrent_jobs: int | None = None,
        stale_after_seconds: int = STALE_AFTER_SECONDS,
        poll_interval_seconds: float = 0.5,
    ) -> None:
        self.job_queue = job_queue
        self.video_processing = video_processing
        self.max_concurrent_jobs = (
            settings.max_concurrent_jobs if max_concurrent_jobs is None else max_concurrent_jobs
        )
        self.stale_after_seconds = stale_after_seconds
        self.poll_interval_seconds = poll_interval_seconds
        if self.max_concurrent_jobs < 1:
            raise ValueError("MAX_CONCURRENT_JOBS must be at least 1")
        if self.stale_after_seconds < 3:
            raise ValueError("JOB_STALE_AFTER_SECONDS must be at least 3")
        self.worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run_forever, name="videomind-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
            if self._thread.is_alive():
                logger.warning("VideoMind worker did not stop before the shutdown timeout")

    def run_forever(self) -> None:
        active: set[Future[None]] = set()
        with ThreadPoolExecutor(max_workers=self.max_concurrent_jobs, thread_name_prefix="videomind-job") as executor:
            while not self._stop.is_set():
                active = {future for future in active if not future.done()}
                try:
                    self.job_queue.recover_stale(self.stale_after_seconds)
                    while len(active) < self.max_concurrent_jobs:
                        claim_id = f"{self.worker_id}-{uuid4().hex[:8]}"
                        job = self.job_queue.claim_next(claim_id)
                        if job is None:
                            break
                        active.add(executor.submit(self._run_claimed, job, claim_id))
                except Exception:
                    logger.exception("Worker queue polling failed")
                self._stop.wait(self.poll_interval_seconds)

    def _run_claimed(self, job: JobRecord, claim_id: str) -> None:
        stopped = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(job, claim_id, stopped),
            name=f"heartbeat-{job.video_id}",
            daemon=True,
        )
        heartbeat.start()
        try:
            self.video_processing.process_claimed(job, claim_id)
        except Exception as exc:
            logger.exception("Unhandled worker failure for video %s", job.video_id)
            if not self.job_queue.fail(job.id, claim_id, str(exc)):
                logger.error("Could not mark job %s failed after an unhandled worker error", job.id)
        finally:
            stopped.set()
            heartbeat.join()

    def _heartbeat_loop(self, job: JobRecord, claim_id: str, stopped: threading.Event) -> None:
        interval = max(1.0, min(15.0, self.stale_after_seconds / 3))
        while not stopped.wait(interval):
            try:
                if not self.job_queue.heartbeat(job.id, claim_id):
                    logger.warning("Worker lost ownership of job %s", job.id)
                    return
            except Exception:
                logger.exception("Heartbeat failed for video %s", job.video_id)


def main() -> None:
    from backend.app.container import get_container

    container = get_container()
    runner = WorkerRunner(container.job_queue, container.video_processing)
    try:
        runner.run_forever()
    except KeyboardInterrupt:
        logger.info("Worker shutdown requested")


if __name__ == "__main__":
    main()
