from pathlib import Path

from sqlalchemy import select

from backend.app.db.models import Job, MediaAsset, Video
from backend.app.repositories.video import VideoRepo
from scripts.migrate_videos_to_assets import migrate_videos_to_assets
from tests.database import create_database, seed_user
from tests.fakes import FakeBlobStore, FakeVectorStore


def test_asset_migration_is_idempotent_and_cleans_duplicate_artifacts(tmp_path: Path) -> None:
    database = create_database(tmp_path)
    seed_user(database, "alice")
    repository = VideoRepo(database.session_factory)
    canonical_id = "1" * 32
    duplicate_id = "2" * 32
    repository.register(canonical_id, "alice")
    repository.register(duplicate_id, "alice")
    data_dir = tmp_path / "data"
    blob_store = FakeBlobStore(data_dir)
    vector_store = FakeVectorStore()
    content = b"same legacy video content"
    blob_store.put(f"videos/{canonical_id}.mp4", content)
    blob_store.put(f"videos/{duplicate_id}.mp4", content)
    blob_store.put(f"transcripts/{canonical_id}.json", "{}")
    blob_store.put(f"summaries/{canonical_id}.json", "{}")
    blob_store.put(f"transcripts/{duplicate_id}.json", "duplicate transcript")
    vector_store.upsert(duplicate_id, [{"id": "legacy"}])
    with database.session_factory.begin() as session:
        session.get(Video, canonical_id).status = "completed"

    counts = migrate_videos_to_assets(
        data_dir,
        database,
        blob_store=blob_store,
        vector_store=vector_store,
    )

    assert counts == {"linked": 2, "duplicates": 1, "skipped_missing_source": 0}
    with database.session_factory() as session:
        videos = list(session.scalars(select(Video).order_by(Video.id)).all())
        assets = list(session.scalars(select(MediaAsset)).all())
        assert len(assets) == 1
        assert assets[0].id == canonical_id
        assert assets[0].status == "queued"
        assert all(video.asset_id == canonical_id for video in videos)
        assert session.scalar(select(Job.state).where(Job.video_id == canonical_id)) == "queued"
    assert blob_store.exists(f"videos/{canonical_id}.mp4")
    assert blob_store.exists(f"transcripts/{canonical_id}.json")
    assert not blob_store.exists(f"videos/{duplicate_id}.mp4")
    assert not blob_store.exists(f"transcripts/{duplicate_id}.json")
    assert duplicate_id not in vector_store.chunks

    rerun = migrate_videos_to_assets(
        data_dir,
        database,
        blob_store=blob_store,
        vector_store=vector_store,
    )
    assert rerun == {"linked": 0, "duplicates": 0, "skipped_missing_source": 0}
    database.dispose()


def test_asset_migration_dry_run_does_not_change_database_or_files(tmp_path: Path) -> None:
    database = create_database(tmp_path)
    seed_user(database, "alice")
    video_id = "3" * 32
    VideoRepo(database.session_factory).register(video_id, "alice")
    data_dir = tmp_path / "data"
    blob_store = FakeBlobStore(data_dir)
    vector_store = FakeVectorStore()
    blob_store.put(f"videos/{video_id}.mp4", b"legacy video")

    counts = migrate_videos_to_assets(
        data_dir,
        database,
        dry_run=True,
        blob_store=blob_store,
        vector_store=vector_store,
    )

    assert counts == {"linked": 1, "duplicates": 0, "skipped_missing_source": 0}
    with database.session_factory() as session:
        assert session.get(Video, video_id).asset_id is None
        assert session.scalar(select(MediaAsset.id)) is None
    assert blob_store.exists(f"videos/{video_id}.mp4")
    database.dispose()


def test_asset_migration_preserves_complete_artifacts_as_completed(tmp_path: Path) -> None:
    database = create_database(tmp_path)
    seed_user(database, "alice")
    video_id = "5" * 32
    VideoRepo(database.session_factory).register(video_id, "alice")
    with database.session_factory.begin() as session:
        session.get(Video, video_id).status = "completed"
    data_dir = tmp_path / "data"
    blob_store = FakeBlobStore(data_dir)
    blob_store.put(f"videos/{video_id}.mp4", b"legacy video")
    for key in (
        f"transcripts/{video_id}.json",
        f"transcripts/{video_id}.chunks.json",
        f"transcripts/{video_id}.embeddings.json",
        f"summaries/{video_id}.json",
    ):
        blob_store.put(key, "{}")
    vector_store = FakeVectorStore()
    vector_store.upsert(video_id, [{"id": "chunk"}])

    migrate_videos_to_assets(
        data_dir,
        database,
        blob_store=blob_store,
        vector_store=vector_store,
    )

    with database.session_factory() as session:
        assert session.get(MediaAsset, video_id).status == "completed"
        assert session.scalar(select(Job.state).where(Job.video_id == video_id)) == "completed"
    database.dispose()
