import json
from pathlib import Path

import pytest
from sqlalchemy import select

from backend.app.db.models import MediaAsset, User, Video
from evals.run_retrieval_eval import (
    EvaluationRow,
    collect_candidates,
    is_placeholder,
    load_dataset,
    resolve_artifact_id,
    score_retrieval,
)
from tests.database import create_database, seed_user


def test_load_dataset_skips_blank_lines_and_reads_expected_schema(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text(
        '\n{"video_id":"abc","question":"When?","expected_answer_keywords":["launch"],'
        '"expected_start_s":10,"expected_end_s":20}\n',
        encoding="utf-8",
    )

    rows = load_dataset(dataset)

    assert rows == [EvaluationRow("abc", "When?", ("launch",), 10.0, 20.0)]


def test_placeholder_dataset_rows_are_explicitly_detected() -> None:
    row = EvaluationRow("PLACEHOLDER_VIDEO_ID", "Question", ("answer",), 0.0, 1.0)
    assert is_placeholder(row)


def test_retrieval_metrics_use_time_overlap_rank_and_empty_fallback() -> None:
    rows = [
        EvaluationRow("one", "question", ("keyword",), 10.0, 20.0),
        EvaluationRow("two", "question", ("keyword",), 30.0, 40.0),
    ]
    chunks = [
        [
            {"metadata": {"start": 0.0, "end": 5.0}},
            {"metadata": {"start": 19.0, "end": 25.0}},
        ],
        [],
    ]

    metrics = score_retrieval(list(zip(rows, chunks)))

    assert metrics.recall_at_5 == 0.5
    assert metrics.mrr == 0.25
    assert metrics.raw_transcript_fallback_fraction == 0.5
    assert metrics.question_count == 2


def test_invalid_dataset_row_reports_line_number(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text(json.dumps({"video_id": "missing-fields"}) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"dataset\.jsonl:1: invalid retrieval-eval row"):
        load_dataset(dataset)


def test_video_id_resolves_to_shared_artifact_id(tmp_path: Path) -> None:
    database = create_database(tmp_path)
    seed_user(database, "alice")
    asset_id = "a" * 32
    video_id = "b" * 32
    with database.session_factory.begin() as session:
        session.add(
            MediaAsset(
                id=asset_id,
                sha256="c" * 64,
                size=1,
                duration_s=1.0,
                blob_key=f"videos/assets/{asset_id}.mp4",
                status="completed",
            )
        )
        session.flush()
        session.add(
            Video(
                id=video_id,
                user_id=session.scalar(select(User.id)),
                asset_id=asset_id,
                source_type="upload",
                status="completed",
            )
        )
    assert resolve_artifact_id(video_id, database.session_factory) == asset_id
    assert resolve_artifact_id("youtube-video-id", database.session_factory) == "youtube-video-id"

    class BlobStore:
        def exists(self, key: str) -> bool:
            return key == f"transcripts/{asset_id}.json"

    class Embedder:
        def embed(self, texts: list[str]) -> list[list[float]]:
            assert texts == ["What happened?"]
            return [[0.1, 0.2]]

    class VectorStore:
        queried: tuple[str, int] | None = None

        def exists(self, collection_id: str) -> bool:
            return collection_id == asset_id

        def query(self, collection_id: str, _embedding: list[float], top_k: int) -> list[dict[str, object]]:
            self.queried = (collection_id, top_k)
            return [{"text": "Evidence", "metadata": {"start": 10.0, "end": 20.0}}]

    vector_store = VectorStore()
    rag = type(
        "Rag",
        (),
        {
            "blob_store": BlobStore(),
            "embedder": Embedder(),
            "vector_store": vector_store,
        },
    )()
    row = EvaluationRow(video_id, "What happened?", ("event",), 10.0, 20.0)
    candidates = collect_candidates(rag, row, database.session_factory)

    assert candidates[0]["text"] == "Evidence"
    assert vector_store.queried == (asset_id, 5)
    database.dispose()
