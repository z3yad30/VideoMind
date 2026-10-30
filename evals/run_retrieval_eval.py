"""Evaluate real VideoMind retrieval against timestamp-annotated questions."""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from sqlalchemy.orm import Session as OrmSession

from backend.app.container import get_container
from backend.app.db.database import get_database
from backend.app.db.models import Video
from backend.app.services.rag import DEFAULT_TOP_K, TranscriptRAGService


THRESHOLDS = (0.55, 0.60, 0.65, 0.70, 0.75, 0.80)
PLACEHOLDER_MARKER = "PLACEHOLDER"


@dataclass(frozen=True)
class EvaluationRow:
    video_id: str
    question: str
    expected_answer_keywords: tuple[str, ...]
    expected_start_s: float
    expected_end_s: float


@dataclass(frozen=True)
class Metrics:
    recall_at_5: float
    mrr: float
    raw_transcript_fallback_fraction: float
    question_count: int


def load_dataset(path: Path) -> list[EvaluationRow]:
    rows: list[EvaluationRow] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            if not isinstance(data, dict):
                raise TypeError("row must be a JSON object")
            keywords = data["expected_answer_keywords"]
            if not isinstance(keywords, list) or not all(isinstance(item, str) for item in keywords):
                raise TypeError("expected_answer_keywords must be a list of strings")
            row = EvaluationRow(
                video_id=str(data["video_id"]),
                question=str(data["question"]),
                expected_answer_keywords=tuple(keywords),
                expected_start_s=float(data["expected_start_s"]),
                expected_end_s=float(data["expected_end_s"]),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{path}:{line_number}: invalid retrieval-eval row: {exc}") from exc
        if not row.video_id.strip() or not row.question.strip() or not row.expected_answer_keywords:
            raise ValueError(f"{path}:{line_number}: video_id, question and keywords must not be empty")
        if (
            not math.isfinite(row.expected_start_s)
            or not math.isfinite(row.expected_end_s)
            or row.expected_start_s < 0
            or row.expected_end_s < row.expected_start_s
        ):
            raise ValueError(f"{path}:{line_number}: expected time range is invalid")
        rows.append(row)
    if not rows:
        raise ValueError(f"{path}: dataset contains no rows")
    return rows


def is_placeholder(row: EvaluationRow) -> bool:
    fields = (row.video_id, row.question, *row.expected_answer_keywords)
    return any(PLACEHOLDER_MARKER in value.upper() for value in fields)


def overlaps_expected_range(
    chunk: dict[str, object],
    expected_start_s: float,
    expected_end_s: float,
) -> bool:
    metadata = chunk.get("metadata")
    if not isinstance(metadata, dict):
        return False
    try:
        start = float(metadata["start"])
        end = float(metadata["end"])
    except (KeyError, TypeError, ValueError):
        return False
    return start <= expected_end_s and end >= expected_start_s


def score_retrieval(
    rows_and_chunks: Sequence[tuple[EvaluationRow, list[dict[str, object]]]],
) -> Metrics:
    if not rows_and_chunks:
        raise ValueError("No non-placeholder evaluation rows remain")
    hits = 0
    reciprocal_rank_total = 0.0
    fallbacks = 0
    for row, chunks in rows_and_chunks:
        if not chunks:
            fallbacks += 1
        first_relevant_rank = next(
            (
                rank
                for rank, chunk in enumerate(chunks, start=1)
                if overlaps_expected_range(chunk, row.expected_start_s, row.expected_end_s)
            ),
            None,
        )
        if first_relevant_rank is not None:
            hits += 1
            reciprocal_rank_total += 1.0 / first_relevant_rank
    count = len(rows_and_chunks)
    return Metrics(
        recall_at_5=hits / count,
        mrr=reciprocal_rank_total / count,
        raw_transcript_fallback_fraction=fallbacks / count,
        question_count=count,
    )


def collect_candidates(
    rag: TranscriptRAGService,
    row: EvaluationRow,
    session_factory: Callable[[], OrmSession] | None = None,
) -> list[dict[str, object]]:
    artifact_id = resolve_artifact_id(row.video_id, session_factory)
    transcript_key = f"transcripts/{artifact_id}.json"
    if not rag.blob_store.exists(transcript_key):
        raise FileNotFoundError(
            f"Video {row.video_id!r} has no transcript artifact at data/{transcript_key}"
        )
    if not rag.vector_store.exists(artifact_id):
        raise FileNotFoundError(
            f"Video {row.video_id!r} has no indexed vector collection for artifact {artifact_id!r}"
        )
    embeddings = rag.embedder.embed([row.question])
    if len(embeddings) != 1:
        raise RuntimeError(f"Embedder returned {len(embeddings)} vectors for one query")
    return rag.vector_store.query(artifact_id, embeddings[0], DEFAULT_TOP_K)


def resolve_artifact_id(
    video_id: str,
    session_factory: Callable[[], OrmSession] | None = None,
) -> str:
    """Map the API video ID to the asset ID used by transcript and vector artifacts."""
    factory = session_factory or get_database().session_factory
    with factory() as session:
        video = session.get(Video, video_id)
        return video.asset_id if video is not None and video.asset_id else video_id


def evaluate(
    rag: TranscriptRAGService,
    rows: Sequence[EvaluationRow],
    session_factory: Callable[[], OrmSession] | None = None,
) -> list[tuple[float, Metrics]]:
    usable_rows = [row for row in rows if not is_placeholder(row)]
    if not usable_rows:
        raise ValueError(
            "The dataset contains only placeholder rows. Replace them with real indexed video examples."
        )
    candidates_by_row = [
        (row, collect_candidates(rag, row, session_factory))
        for row in usable_rows
    ]
    results: list[tuple[float, Metrics]] = []
    for threshold in THRESHOLDS:
        filtered = [
            (
                row,
                TranscriptRAGService.filter_relevant_chunks(
                    candidates,
                    min_similarity=threshold,
                ),
            )
            for row, candidates in candidates_by_row
        ]
        results.append((threshold, score_retrieval(filtered)))
    return results


def print_results(results: Sequence[tuple[float, Metrics]]) -> None:
    print(f"{'MIN_SIMILARITY':>14}  {'RECALL@5':>9}  {'MRR':>7}  {'RAW FALLBACK':>12}  {'QUESTIONS':>9}")
    print("-" * 62)
    for threshold, metrics in results:
        print(
            f"{threshold:14.2f}  "
            f"{metrics.recall_at_5:9.3f}  "
            f"{metrics.mrr:7.3f}  "
            f"{metrics.raw_transcript_fallback_fraction:12.3f}  "
            f"{metrics.question_count:9d}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path(__file__).with_name("dataset.jsonl"),
        help="JSONL evaluation dataset (default: evals/dataset.jsonl)",
    )
    args = parser.parse_args(argv)
    try:
        rows = load_dataset(args.dataset)
        usable_count = sum(not is_placeholder(row) for row in rows)
        if not usable_count:
            print(
                f"{args.dataset}: replace the 3 placeholder rows with real examples before evaluating.",
                file=sys.stderr,
            )
            return 2
        results = evaluate(get_container().rag, rows)
    except (OSError, ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"Retrieval evaluation failed: {exc}", file=sys.stderr)
        return 1
    print_results(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
