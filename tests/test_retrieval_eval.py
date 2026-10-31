import json
from pathlib import Path

import pytest
from sqlalchemy import select

from backend.app.db.models import MediaAsset, User, Video
from evals.run_retrieval_eval import (
    EvaluationRow,
    collect_candidates,
    evaluate,
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


def test_eval_rows_accept_optional_prior_messages_for_rewrite_measurement(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "video_id": "video",
                "question": "When did it happen?",
                "expected_answer_keywords": ["launch"],
                "expected_start_s": 1,
                "expected_end_s": 2,
                "prior_messages": [{"role": "assistant", "content": "The launch was discussed."}],
            }
        ),
        encoding="utf-8",
    )

    assert load_dataset(dataset)[0].prior_messages == (
        {"role": "assistant", "content": "The launch was discussed."},
    )


def test_eval_compares_each_flag_against_baseline(tmp_path: Path) -> None:
    database = create_database(tmp_path)
    row = EvaluationRow(
        "video",
        "When did it happen?",
        ("launch",),
        1.0,
        2.0,
        ({"role": "assistant", "content": "The launch was discussed."},),
    )

    class BlobStore:
        def exists(self, _key: str) -> bool:
            return True

    class VectorStore:
        def exists(self, _video_id: str) -> bool:
            return True

    class Rag:
        blob_store = BlobStore()
        vector_store = VectorStore()

        def __init__(self) -> None:
            self.calls = []

        def retrieve_candidates(
            self,
            _video_id,
            question,
            _top_k,
            *,
            hybrid_enabled,
            rerank_enabled,
        ):
            self.calls.append((question, hybrid_enabled, rerank_enabled))
            relevant = question == "standalone query" or hybrid_enabled or rerank_enabled
            start = 1.0 if relevant else 10.0
            return [{
                "text": "candidate",
                "metadata": {"start": start, "end": start + 1.0},
                "similarity": 0.9,
            }]

    rag = Rag()
    results = evaluate(
        rag,
        [row],
        database.session_factory,
        rewrite_query=lambda _question, _history: "standalone query",
    )

    assert list(results) == ["baseline", "QUERY_REWRITE", "HYBRID", "RERANK"]
    assert results["baseline"][0][1].recall_at_5 == 0.0
    assert results["QUERY_REWRITE"][0][1].recall_at_5 == 1.0
    assert results["HYBRID"][0][1].recall_at_5 == 1.0
    assert results["RERANK"][0][1].recall_at_5 == 1.0
    assert rag.calls[0] == ("When did it happen?", False, False)
    assert rag.calls[1] == ("standalone query", False, False)
    database.dispose()


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

    def retrieve_candidates(_self, collection_id, question, top_k, **_kwargs):
        assert question == "What happened?"
        embedding = [0.1, 0.2]
        return vector_store.query(collection_id, embedding, top_k)

    rag = type(
        "Rag",
        (),
        {
            "blob_store": BlobStore(),
            "embedder": Embedder(),
            "vector_store": vector_store,
            "retrieve_candidates": retrieve_candidates,
        },
    )()
    row = EvaluationRow(video_id, "What happened?", ("event",), 10.0, 20.0)
    candidates = collect_candidates(rag, row, database.session_factory)

    assert candidates[0]["text"] == "Evidence"
    assert vector_store.queried == (asset_id, 5)
    database.dispose()
