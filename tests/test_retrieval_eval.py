import json
from pathlib import Path

import pytest

from evals.run_retrieval_eval import (
    EvaluationRow,
    is_placeholder,
    load_dataset,
    score_retrieval,
)


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
