import json
from pathlib import Path

import pytest

from backend.app.services.rag import TranscriptRAGService


class FakeEmbeddings:
    def encode(self, sentences: list[str]):
        return [[float(len(sentence)), float(sentence.lower().count("python"))] for sentence in sentences]


def make_service(tmp_path: Path) -> TranscriptRAGService:
    return TranscriptRAGService(
        transcript_dir=tmp_path / "transcripts",
        chroma_dir=tmp_path / "chroma",
        embedding_model=FakeEmbeddings(),
    )


def write_transcript(tmp_path: Path, video_id: str, segments: list[dict[str, object]]) -> None:
    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    (transcript_dir / f"{video_id}.json").write_text(
        json.dumps({"video_id": video_id, "segments": segments}), encoding="utf-8"
    )


def test_video_collections_are_isolated(tmp_path: Path) -> None:
    class MatchingCollection:
        def query(self, **kwargs):
            return {
                "documents": [["Cooking uses heat"]],
                "metadatas": [[{"video_id": "b", "start": 3.0, "end": 4.0}]],
                "distances": [[0.12]],
            }

    class MatchingClient:
        def get_collection(self, name):
            assert name == "video_b"
            return MatchingCollection()

    write_transcript(tmp_path, "a", [{"text": "Python is a language", "start": 1, "end": 2}])
    write_transcript(tmp_path, "b", [{"text": "Cooking uses heat", "start": 3, "end": 4}])
    service = TranscriptRAGService(
        transcript_dir=tmp_path / "transcripts",
        chroma_dir=tmp_path / "chroma",
        embedding_model=FakeEmbeddings(),
        chroma_client=MatchingClient(),
    )

    results = service.retrieve_relevant_chunks("b", "Python language")

    assert results[0]["metadata"]["video_id"] == "b"
    assert "Cooking" in results[0]["text"]


def test_metadata_timestamps_survive_storage_and_retrieval(tmp_path: Path) -> None:
    class MatchingCollection:
        def query(self, **kwargs):
            return {
                "documents": [["A timestamped fact"]],
                "metadatas": [[{"video_id": "a", "chunk_id": "0", "start": 12.5, "end": 18.75, "source": str(tmp_path / "transcripts" / "a.json")}]],
                "distances": [[0.08]],
            }

    class MatchingClient:
        def get_collection(self, name):
            assert name == "video_a"
            return MatchingCollection()

    write_transcript(tmp_path, "a", [{"text": "A timestamped fact", "start": 12.5, "end": 18.75}])
    service = TranscriptRAGService(
        transcript_dir=tmp_path / "transcripts",
        chroma_dir=tmp_path / "chroma",
        embedding_model=FakeEmbeddings(),
        chroma_client=MatchingClient(),
    )

    result = service.retrieve_relevant_chunks("a", "timestamped fact")[0]

    assert result["metadata"] == {
        "video_id": "a",
        "chunk_id": "0",
        "start": 12.5,
        "end": 18.75,
        "source": str(tmp_path / "transcripts" / "a.json"),
    }


def test_relevant_chunks_and_context_are_returned(tmp_path: Path) -> None:
    class MatchingCollection:
        def query(self, **kwargs):
            return {
                "documents": [["Python makes embeddings"]],
                "metadatas": [[{"video_id": "a", "start": 2.0, "end": 3.0}]],
                "distances": [[0.12]],
            }

    class MatchingClient:
        def get_collection(self, name):
            assert name == "video_a"
            return MatchingCollection()

    write_transcript(
        tmp_path,
        "a",
        [
            {"text": "The cat sleeps", "start": 0, "end": 1},
            {"text": "Python makes embeddings", "start": 2, "end": 3},
        ],
    )
    service = TranscriptRAGService(
        transcript_dir=tmp_path / "transcripts",
        chroma_dir=tmp_path / "chroma",
        embedding_model=FakeEmbeddings(),
        chroma_client=MatchingClient(),
        max_chunk_characters=20,
    )

    results = service.retrieve_relevant_chunks("a", "How does Python work?", top_k=1)

    assert len(results) == 1
    assert "Python" in results[0]["text"]
    assert "2.0-3.0" in service.build_rag_context("a", "How does Python work?", top_k=1)


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
    class ContaminatedCollection:
        def query(self, **kwargs):
            return {
                "documents": [["Video B secret"]],
                "metadatas": [[{"video_id": "b", "start": 0.0, "end": 1.0}]],
                "distances": [[0.1]],
            }

    class ContaminatedClient:
        def get_collection(self, name):
            assert name == "video_a"
            return ContaminatedCollection()

    service = TranscriptRAGService(
        transcript_dir=tmp_path / "transcripts",
        chroma_dir=tmp_path / "chroma",
        embedding_model=FakeEmbeddings(),
        chroma_client=ContaminatedClient(),
    )

    assert service.retrieve_relevant_chunks("a", "anything") == []


def test_retrieval_surfaces_backend_failures(tmp_path: Path) -> None:
    class BrokenCollection:
        def query(self, **kwargs):
            raise RuntimeError("embedding backend unavailable")

    class BrokenClient:
        def get_collection(self, name):
            return BrokenCollection()

    service = TranscriptRAGService(
        transcript_dir=tmp_path / "transcripts",
        chroma_dir=tmp_path / "chroma",
        embedding_model=FakeEmbeddings(),
        chroma_client=BrokenClient(),
    )
    write_transcript(tmp_path, "a", [{"text": "indexed transcript", "start": 0, "end": 1}])

    with pytest.raises(RuntimeError, match="embedding backend unavailable"):
        service.retrieve_relevant_chunks("a", "anything")


def test_sentence_transformer_load_failure_uses_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    import backend.app.services.rag as rag_module

    class FakeSentenceTransformer:
        def __init__(self, *_args, **_kwargs):
            raise OSError("BAAI/bge-m3 does not appear to have a file named pytorch_model.bin")

    fake_mod = type("FakeMod", (), {"SentenceTransformer": FakeSentenceTransformer})
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_mod)

    service = rag_module.SentenceTransformerEmbedding()
    vectors = service.encode(["alpha beta", "gamma"])

    assert isinstance(vectors, list)
    assert len(vectors) == 2
    assert len(vectors[0]) == 32