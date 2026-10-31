# Retrieval evaluation

This harness measures the real video-scoped retriever and compares the
baseline with `QUERY_REWRITE`, `HYBRID`, and `RERANK`, one feature at a time.
It does not change the application's configured retrieval policy. It uses the
configured embedding model and vector store from `backend/app/container.py`,
and queries the saved index for each dataset question. The first run may load
or download the embedding model; enabling `RERANK` for the evaluation also
loads its configured CrossEncoder model.

## Dataset

`dataset.jsonl` has three example rows marked as placeholders. Replace them
with 30–50 examples for videos that have already been processed and indexed.
Each non-empty line must be a JSON object with this shape:

```json
{"video_id":"32-character-video-id","question":"What was the conclusion?","expected_answer_keywords":["conclusion","result"],"expected_start_s":125.0,"expected_end_s":151.5}
```

For questions that depend on earlier chat turns, optionally add the prior
conversation messages (the latest three are used by query rewriting):

```json
{"video_id":"32-character-video-id","question":"When was that?","expected_answer_keywords":["March"],"expected_start_s":125.0,"expected_end_s":151.5,"prior_messages":[{"role":"user","content":"When was the launch discussed?"},{"role":"assistant","content":"The launch was discussed in the keynote."}]}
```

Rows without `prior_messages` still participate in all variants, but
`QUERY_REWRITE` uses their original question and cannot measure conversational
rewriting for those rows.

- `video_id` is the API-facing video ID from the videos table. For uploaded
  videos, the harness resolves it to the shared `asset_id` used by transcript
  files and the Chroma collection; YouTube video IDs resolve to themselves.
  The matching transcript and vector collection must exist locally.
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

The report prints results for baseline and each single-feature variant, and
sweeps `MIN_SIMILARITY` from `0.55` through `0.80` in `0.05` increments. For
each threshold and variant:

- **Recall@5** is the fraction of questions with at least one returned chunk
  whose `[start, end]` intersects the expected time interval.
- **MRR** is the mean reciprocal rank of the first returned chunk overlapping
  that interval; a miss contributes zero.
- **Raw fallback** is the fraction of questions for which threshold filtering
  returns no chunks, matching the API path that falls back to raw transcript
  context.

Each variant retrieves candidates with the real retriever, then the existing
`TranscriptRAGService` threshold filter is applied at each sweep value.
Baseline and query-rewrite use dense retrieval; hybrid fuses dense and lexical
rankings; reranking fetches up to 20 candidates before selecting the top five.
Recall and MRR are temporal measures. Review
`expected_answer_keywords` alongside the table when judging whether retrieved
content actually answers each question.

The features are independently opt-in at runtime and default to `false`:

- `QUERY_REWRITE=true` makes one LLM call to rewrite a contextual question
  before retrieval when prior chat history exists. Rewrite failures use the
  original question.
- `HYBRID=true` merges dense and BM25 rankings using reciprocal rank fusion.
- `RERANK=true` reranks the top 20 candidates to the top 5 with the configured
  `RERANK_MODEL`.

The comparison harness evaluates retrieval only; it does not make an automatic
recommendation or change defaults. The checked-in dataset contains placeholders,
not measured examples. Add real questions and indexed videos before drawing
conclusions. Enable a feature by default only after real evaluation shows
higher Recall@5 without a higher raw-transcript fallback fraction.
