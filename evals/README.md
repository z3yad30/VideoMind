# Retrieval evaluation

This harness measures the existing video-scoped retriever; it does not change
the application retrieval policy. It uses the configured real embedding model
and vector store from `backend/app/container.py`, and queries the saved index
for each dataset question. The first run may load or download the embedding
model.

## Dataset

`dataset.jsonl` has three example rows marked as placeholders. Replace them
with 30–50 examples for videos that have already been processed and indexed.
Each non-empty line must be a JSON object with this shape:

```json
{"video_id":"32-character-video-id","question":"What was the conclusion?","expected_answer_keywords":["conclusion","result"],"expected_start_s":125.0,"expected_end_s":151.5}
```

- `video_id` identifies a video whose transcript and vector collection exist.
- `question` is the natural-language retrieval query.
- `expected_answer_keywords` records expected answer content for human review;
  it is not part of the temporal ranking metrics.
- `expected_start_s` and `expected_end_s` bound the transcript interval that
  contains supporting evidence.

Rows containing `PLACEHOLDER` in the video ID, question, or keywords are
ignored. The harness stops with an explanatory error if no real rows remain.

## Run

From the repository root, with the project environment and dependencies set up:

```cmd
python -m evals.run_retrieval_eval
```

To select another JSONL file:

```cmd
python -m evals.run_retrieval_eval --dataset path\to\my_dataset.jsonl
```

The report sweeps `MIN_SIMILARITY` from `0.55` through `0.80` in `0.05`
increments. For each threshold:

- **Recall@5** is the fraction of questions with at least one returned chunk
  whose `[start, end]` intersects the expected time interval.
- **MRR** is the mean reciprocal rank of the first returned chunk overlapping
  that interval; a miss contributes zero.
- **Raw fallback** is the fraction of questions for which threshold filtering
  returns no chunks, matching the API path that falls back to raw transcript
  context.

The vector store is queried once per question for its top five real candidates;
the existing `TranscriptRAGService` threshold filter is then applied at each
sweep value. Recall and MRR are temporal measures. Review
`expected_answer_keywords` alongside the table when judging whether retrieved
content actually answers each question.
