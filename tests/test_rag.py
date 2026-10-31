from pathlib import Path

import pytest

from backend.app.adapters.embeddings import SentenceTransformerEmbedder
from backend.app.services.rag import TranscriptRAGService
from tests.fakes import FakeBlobStore, FakeEmbedder, FakeReranker, FakeVectorStore


def make_service(tmp_path: Path) -> TranscriptRAGService:
    return TranscriptRAGService(FakeBlobStore(tmp_path / "data"), FakeEmbedder(), FakeVectorStore())


def write_transcript(tmp_path: Path, video_id: str, segments: list[dict[str, object]]) -> None:
    FakeBlobStore(tmp_path / "data").write_transcript(video_id, segments)


def test_video_collections_are_isolated(tmp_path: Path) -> None:
    write_transcript(tmp_path, "a", [{"text": "Python is a language", "start": 1, "end": 2}])
    write_transcript(tmp_path, "b", [{"text": "Cooking uses heat", "start": 3, "end": 4}])
    service = TranscriptRAGService(
        FakeBlobStore(tmp_path / "data"),
        FakeEmbedder(),
        FakeVectorStore([{
            "text": "Cooking uses heat",
            "metadata": {"video_id": "b", "start": 3.0, "end": 4.0},
            "similarity": 0.88,
        }]),
    )

    results = service.retrieve_relevant_chunks("b", "Python language")

    assert results[0]["metadata"]["video_id"] == "b"
    assert "Cooking" in results[0]["text"]


def test_metadata_timestamps_survive_storage_and_retrieval(tmp_path: Path) -> None:
    transcript_path = tmp_path / "data" / "transcripts" / "a.json"
    write_transcript(tmp_path, "a", [{"text": "A timestamped fact", "start": 12.5, "end": 18.75}])
    service = TranscriptRAGService(
        FakeBlobStore(tmp_path / "data"),
        FakeEmbedder(),
        FakeVectorStore([{
            "text": "A timestamped fact",
            "metadata": {"video_id": "a", "chunk_id": "0", "start": 12.5, "end": 18.75, "source": str(transcript_path)},
            "similarity": 0.92,
        }]),
    )

    result = service.retrieve_relevant_chunks("a", "timestamped fact")[0]

    assert result["metadata"] == {
        "video_id": "a",
        "chunk_id": "0",
        "start": 12.5,
        "end": 18.75,
        "source": str(transcript_path),
    }


def test_relevant_chunks_and_context_are_returned(tmp_path: Path) -> None:
    write_transcript(
        tmp_path,
        "a",
        [
            {"text": "The cat sleeps", "start": 0, "end": 1},
            {"text": "Python makes embeddings", "start": 2, "end": 3},
        ],
    )
    service = TranscriptRAGService(
        FakeBlobStore(tmp_path / "data"),
        FakeEmbedder(),
        FakeVectorStore([{
            "text": "Python makes embeddings",
            "metadata": {"video_id": "a", "start": 2.0, "end": 3.0},
            "similarity": 0.9,
        }]),
        max_chunk_characters=20,
    )

    results = service.retrieve_relevant_chunks("a", "How does Python work?", top_k=1)

    assert len(results) == 1
    assert "Python" in results[0]["text"]
    assert "2.0-3.0" in service.build_rag_context("a", "How does Python work?", top_k=1)


def test_hybrid_flag_routes_query_through_vector_store_port(tmp_path: Path) -> None:
    write_transcript(tmp_path, "a", [{"text": "transcript", "start": 0.0, "end": 1.0}])
    vector_store = FakeVectorStore([{
        "text": "Hybrid result",
        "metadata": {"video_id": "a", "start": 0.0, "end": 1.0},
        "similarity": 0.9,
    }])
    service = TranscriptRAGService(FakeBlobStore(tmp_path / "data"), FakeEmbedder(), vector_store)

    candidates = service.retrieve_candidates("a", "hybrid query", hybrid_enabled=True)

    assert candidates[0]["text"] == "Hybrid result"
    assert vector_store.hybrid_queries[0][2:] == ("hybrid query", 5)


def test_rerank_flag_fetches_twenty_then_returns_five(tmp_path: Path) -> None:
    write_transcript(tmp_path, "a", [{"text": "transcript", "start": 0.0, "end": 1.0}])
    vector_store = FakeVectorStore([
        {
            "id": f"chunk-{index}",
            "text": f"chunk-{index}",
            "metadata": {"video_id": "a", "start": float(index), "end": float(index + 1)},
            "similarity": 0.9,
        }
        for index in range(20)
    ])
    service = TranscriptRAGService(
        FakeBlobStore(tmp_path / "data"),
        FakeEmbedder(),
        vector_store,
        reranker=FakeReranker(),
        rerank_enabled=True,
    )

    candidates = service.retrieve_candidates("a", "question", top_k=5)

    assert vector_store.queries[0][2] == 20
    assert [candidate["text"] for candidate in candidates] == [
        "chunk-19", "chunk-18", "chunk-17", "chunk-16", "chunk-15",
    ]


def test_dynamic_similarity_threshold_keeps_only_strong_matches() -> None:
    service = TranscriptRAGService.__new__(TranscriptRAGService)

    assert [chunk["similarity"] for chunk in service.filter_relevant_chunks([
        {"text": "a", "similarity": 0.91},
        {"text": "b", "similarity": 0.86},
        {"text": "c", "similarity": 0.82},
        {"text": "d", "similarity": 0.60},
        {"text": "e", "similarity": 0.45},
    ])] == [0.91, 0.86, 0.82]

    assert [chunk["similarity"] for chunk in service.filter_relevant_chunks([
        {"text": "a", "similarity": 0.74},
        {"text": "b", "similarity": 0.72},
        {"text": "c", "similarity": 0.71},
        {"text": "d", "similarity": 0.69},
        {"text": "e", "similarity": 0.65},
    ])] == [0.74, 0.72, 0.71]

    assert service.filter_relevant_chunks([
        {"text": "a", "similarity": 0.69},
        {"text": "b", "similarity": 0.67},
        {"text": "c", "similarity": 0.65},
        {"text": "d", "similarity": 0.62},
        {"text": "e", "similarity": 0.60},
    ]) == []


def test_missing_or_empty_collections_are_handled_gracefully(tmp_path: Path) -> None:
    service = make_service(tmp_path)

    assert service.index_transcript("missing") == []
    assert service.retrieve_relevant_chunks("missing", "anything") == []

    write_transcript(tmp_path, "empty", [])
    assert service.index_transcript("empty") == []
    assert service.retrieve_relevant_chunks("empty", "anything") == []


def test_indexing_rejects_an_embedder_result_count_mismatch(tmp_path: Path) -> None:
    class BrokenEmbedder:
        def embed(self, texts: list[str]) -> list[list[float]]:
            return []

    service = TranscriptRAGService(
        FakeBlobStore(tmp_path / "data"),
        BrokenEmbedder(),
        FakeVectorStore(),
    )
    write_transcript(tmp_path, "video", [{"text": "transcript", "start": 0.0, "end": 1.0}])

    with pytest.raises(RuntimeError, match="unexpected number of vectors"):
        service.index_transcript("video")


def test_raw_transcript_fallback_is_bounded_and_question_focused(tmp_path: Path) -> None:
    write_transcript(tmp_path, "a", [
        {"text": "An unrelated introduction.", "start": 0, "end": 1},
        {"text": "Binary classification uses two labels.", "start": 1, "end": 2},
        {"text": "The model predicts one of the two classes.", "start": 2, "end": 3},
        {"text": "x " * 7000, "start": 3, "end": 4},
    ])
    service = make_service(tmp_path)

    context = service.get_transcript_context_for_question("a", "How does binary classification work?")

    assert len(context) <= 12000
    assert "Binary classification uses two labels." in context


def test_retrieval_rejects_chunks_from_another_video(tmp_path: Path) -> None:
    service = TranscriptRAGService(
        FakeBlobStore(tmp_path / "data"),
        FakeEmbedder(),
        FakeVectorStore([{
            "text": "Video B secret",
            "metadata": {"video_id": "b", "start": 0.0, "end": 1.0},
            "similarity": 0.99,
        }]),
    )

    assert service.retrieve_relevant_chunks("a", "anything") == []


def test_retrieval_surfaces_backend_failures(tmp_path: Path) -> None:
    class BrokenVectorStore(FakeVectorStore):
        def query(self, video_id, embedding, top_k):
            raise RuntimeError("embedding backend unavailable")

    service = TranscriptRAGService(
        FakeBlobStore(tmp_path / "data"),
        FakeEmbedder(),
        BrokenVectorStore(),
    )
    write_transcript(tmp_path, "a", [{"text": "indexed transcript", "start": 0, "end": 1}])

    with pytest.raises(RuntimeError, match="embedding backend unavailable"):
        service.retrieve_relevant_chunks("a", "anything")


def test_sentence_transformer_load_failure_uses_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    class FakeSentenceTransformer:
        def __init__(self, *_args, **_kwargs):
            raise OSError("BAAI/bge-m3 does not appear to have a file named pytorch_model.bin")

    fake_mod = type("FakeMod", (), {"SentenceTransformer": FakeSentenceTransformer})
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_mod)

    service = SentenceTransformerEmbedder()
    vectors = service.embed(["alpha beta", "gamma"])

    assert isinstance(vectors, list)
    assert len(vectors) == 2
    assert len(vectors[0]) == 32