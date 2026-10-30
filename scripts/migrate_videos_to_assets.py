from __future__ import annotations

import argparse
import hashlib
import sys
from collections import defaultdict
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from backend.app.core.config import settings
from backend.app.db.database import Database
from backend.app.db.models import Job, MediaAsset, Video
from backend.app.ports import BlobStore, VectorStore
from backend.app.repositories.video import AssetCleanup
from backend.app.services.asset_cleanup import delete_asset_artifacts
from backend.app.services.media import MediaService


class AssetMigrationError(RuntimeError):
    pass


def _fingerprint(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _source_path(data_dir: Path, video_id: str) -> Path | None:
    directory = data_dir / "videos"
    return next(
        (
            candidate
            for candidate in sorted(directory.glob(f"{video_id}.*"))
            if candidate.is_file() and candidate.suffix.lower() in MediaService.supported_extensions
        ),
        None,
    )


def _has_completed_artifacts(data_dir: Path, video: Video, vector_store: VectorStore) -> bool:
    return video.status == "completed" and all(
        (data_dir / key).is_file()
        for key in (
            f"transcripts/{video.id}.json",
            f"transcripts/{video.id}.chunks.json",
            f"transcripts/{video.id}.embeddings.json",
            f"summaries/{video.id}.json",
        )
    ) and vector_store.exists(video.id)


def _artifact_score(data_dir: Path, video: Video, vector_store: VectorStore) -> int:
    return sum(
        (data_dir / key).is_file()
        for key in (
            f"transcripts/{video.id}.json",
            f"transcripts/{video.id}.chunks.json",
            f"transcripts/{video.id}.embeddings.json",
            f"summaries/{video.id}.json",
            f"audio/summaries/{video.id}.wav",
        )
    ) + (6 if _has_completed_artifacts(data_dir, video, vector_store) else 0)


def migrate_videos_to_assets(
    data_dir: Path,
    database: Database,
    *,
    dry_run: bool = False,
    blob_store: BlobStore | None = None,
    vector_store: VectorStore | None = None,
) -> dict[str, int]:
    counts = {"linked": 0, "duplicates": 0, "skipped_missing_source": 0}
    cleanup: list[AssetCleanup] = []
    if vector_store is None:
        from backend.app.container import get_vector_store

        vector_store = get_vector_store()
    try:
        with database.session_factory.begin() as session:
            unlinked = list(
                session.scalars(
                    select(Video).where(Video.asset_id.is_(None)).order_by(Video.created_at, Video.id)
                ).all()
            )
            groups: dict[str, list[tuple[Video, Path, int]]] = defaultdict(list)
            for video in unlinked:
                source = _source_path(data_dir, video.id)
                if source is None:
                    counts["skipped_missing_source"] += 1
                    continue
                digest, size = _fingerprint(source)
                groups[digest].append((video, source, size))

            for digest, entries in groups.items():
                asset = session.scalar(select(MediaAsset).where(MediaAsset.sha256 == digest))
                if asset is None:
                    canonical, source, size = max(
                        entries,
                        key=lambda entry: _artifact_score(data_dir, entry[0], vector_store),
                    )
                    source_key = source.relative_to(data_dir).as_posix()
                    asset = MediaAsset(
                        id=canonical.id,
                        sha256=digest,
                        size=size,
                        duration_s=canonical.duration_s,
                        blob_key=source_key,
                        status="completed"
                        if _has_completed_artifacts(data_dir, canonical, vector_store)
                        else "queued",
                    )
                    session.add(asset)
                    session.flush()
                    canonical_video_id = canonical.id
                else:
                    canonical_video_id = session.scalar(
                        select(Video.id)
                        .where(Video.asset_id == asset.id)
                        .order_by(Video.created_at, Video.id)
                        .limit(1)
                    )
                    if canonical_video_id is None:
                        canonical_video_id = max(
                            entries,
                            key=lambda entry: _artifact_score(data_dir, entry[0], vector_store),
                        )[0].id
                    if any(
                        _has_completed_artifacts(data_dir, video, vector_store)
                        for video, _, _ in entries
                    ):
                        asset.status = "completed"
                for video, source, _size in entries:
                    video.asset_id = asset.id
                    counts["linked"] += 1
                    if video.id == canonical_video_id:
                        continue
                    counts["duplicates"] += 1
                    cleanup.append(
                        AssetCleanup(
                            video.id,
                            source.relative_to(data_dir).as_posix(),
                        )
                    )
                    old_job = session.scalar(select(Job).where(Job.video_id == video.id))
                    if old_job is not None:
                        canonical_job = session.scalar(select(Job.id).where(Job.video_id == canonical_video_id))
                        if canonical_job is None:
                            old_job.video_id = canonical_video_id
                        else:
                            session.delete(old_job)
                canonical_job = session.scalar(
                    select(Job).where(Job.video_id == canonical_video_id)
                )
                if asset.status == "completed":
                    if canonical_job is None:
                        session.add(
                            Job(
                                video_id=canonical_video_id,
                                state="completed",
                                attempts=0,
                                stage="generating_summary_audio",
                            )
                        )
                    else:
                        canonical_job.state = "completed"
                        canonical_job.stage = canonical_job.stage or "generating_summary_audio"
                        canonical_job.error = None
                        canonical_job.locked_by = None
                        canonical_job.locked_at = None
                    session.execute(
                        update(Video).where(Video.asset_id == asset.id).values(status="completed")
                    )
                else:
                    asset.status = "queued"
                    if canonical_job is None:
                        session.add(
                            Job(
                                video_id=canonical_video_id,
                                state="queued",
                                attempts=0,
                                stage=None,
                                error=None,
                            )
                        )
                    else:
                        canonical_job.state = "queued"
                        canonical_job.stage = None
                        canonical_job.error = None
                        canonical_job.locked_by = None
                        canonical_job.locked_at = None
                    session.execute(
                        update(Video).where(Video.asset_id == asset.id).values(status="queued")
                    )

            if dry_run:
                session.rollback()
    except SQLAlchemyError as exc:
        raise AssetMigrationError("Video asset migration failed") from exc

    if dry_run:
        return counts
    if cleanup:
        if blob_store is None:
            from backend.app.container import get_blob_store

            blob_store = get_blob_store()
        for asset in cleanup:
            delete_asset_artifacts(asset, blob_store, vector_store)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Link existing video records to content-addressed media assets."
    )
    parser.add_argument("--dry-run", action="store_true", help="Count the migration without committing changes.")
    parser.add_argument("--data-dir", type=Path, default=settings.project_root / "data")
    parser.add_argument("--database-url", default=settings.database_url)
    args = parser.parse_args(argv)
    database = Database(args.database_url, settings.sqlite_busy_timeout_ms)
    try:
        counts = migrate_videos_to_assets(args.data_dir, database, dry_run=args.dry_run)
        action = "Would link" if args.dry_run else "Linked"
        cleanup_status = "would be cleaned" if args.dry_run else "cleaned"
        print(
            f"{action} {counts['linked']} video(s); "
            f"{counts['duplicates']} duplicate artifact set(s) {cleanup_status}; "
            f"{counts['skipped_missing_source']} video(s) skipped without a source file."
        )
        return 0
    except (AssetMigrationError, OSError, RuntimeError) as exc:
        print(f"Video asset migration failed: {exc}", file=sys.stderr)
        return 1
    finally:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
