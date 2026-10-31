import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.adapters.blob_store import LocalBlobStore
from backend.app.adapters.embeddings import SentenceTransformerEmbedder
from backend.app.adapters.llm import GroqLLMClient
from backend.app.adapters.reranker import SentenceTransformerReranker
from backend.app.adapters.transcriber import FasterWhisperTranscriber
from backend.app.adapters.tts import Pyttsx3TTS
from backend.app.adapters.vector_store import ChromaVectorStore
from backend.app.ports import Segment
import backend.app.container as container_module
from tests.fakes import FakeBlobStore, FakeEmbedder, FakeLLMClient, FakeTTS, FakeTranscriber, FakeVectorStore


def test_fake_adapters_satisfy_each_port(tmp_path: Path) -> None:
    transcriber = FakeTranscriber([Segment("speech", 1.0, 2.0)])
    embedder = FakeEmbedder()
    vector_store = FakeVectorStore()
    llm_client = FakeLLMClient(["reply"])
    tts = FakeTTS()
    blob_store = FakeBlobStore(tmp_path / "fake-data")

    assert transcriber.transcribe(Path("audio.wav")) == [Segment("speech", 1.0, 2.0)]
    assert embedder.embed(["speech"])
    vector_store.upsert("video", [{"id": "0", "text": "speech", "metadata": {}, "embedding": [1.0]}])
    assert vector_store.chunks["video"][0]["text"] == "speech"
    assert llm_client.complete([{"role": "user", "content": "prompt"}]) == "reply"
    assert tts.synthesize("reply", tmp_path / "answer.wav") == tmp_path / "answer.wav"
    assert blob_store.open("source/file.mp4").name == "file.mp4"


def test_local_blob_store_preserves_layout_and_supports_operations(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path / "data")
    path = store.put("videos/example.mp4", b"video")

    assert path == tmp_path / "data" / "videos" / "example.mp4"
    assert store.open("videos/example.mp4").read_bytes() == b"video"
    assert store.exists("videos/example.mp4")
    store.delete("videos/example.mp4")
    assert not store.exists("videos/example.mp4")
    with pytest.raises(ValueError, match="relative path"):
        store.open("../outside")


def test_local_blob_store_deletes_exact_files_and_directory_prefixes(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path / "data")
    store.put("videos/abc123.mp4", b"video")
    store.put("videos/abc123-extra.mp4", b"unrelated")
    store.put("audio/answers/abc123/answer.wav", b"answer")

    store.delete_prefix("videos/abc123.mp4")
    store.delete_prefix("audio/answers/abc123")

    assert not store.exists("videos/abc123.mp4")
    assert store.exists("videos/abc123-extra.mp4")
    assert not store.exists("audio/answers/abc123/answer.wav")


def test_faster_whisper_transcriber_preserves_segments(monkeypatch: pytest.MonkeyPatch) -> None:
    class Model:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def transcribe(self, audio_path: str):
            assert audio_path == "audio.wav"
            return [SimpleNamespace(text=" transcript ", start=0.5, end=2.25)], None

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=Model))

    assert FasterWhisperTranscriber().transcribe(Path("audio.wav")) == [Segment(" transcript ", 0.5, 2.25)]


def test_faster_whisper_retries_missing_cuda_library_on_cpu_int8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_devices: list[tuple[str, str]] = []

    class Model:
        def __init__(self, _name: str, *, device: str, compute_type: str) -> None:
            model_devices.append((device, compute_type))
            self.device = device

        def transcribe(self, _audio_path: str):
            if self.device == "cuda":
                raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
            return [SimpleNamespace(text=" transcript ", start=0.5, end=2.25)], None

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=Model))
    transcriber = FasterWhisperTranscriber(device="cuda", compute_type="float16")

    assert transcriber.transcribe(Path("audio.wav")) == [Segment(" transcript ", 0.5, 2.25)]
    assert model_devices == [("cuda", "float16"), ("cpu", "int8")]
    assert (transcriber.device, transcriber.compute_type) == ("cpu", "int8")


def test_faster_whisper_does_not_fallback_for_unrelated_runtime_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Model:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def transcribe(self, _audio_path: str):
            raise RuntimeError("invalid audio input")

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=Model))

    with pytest.raises(RuntimeError, match="invalid audio input"):
        FasterWhisperTranscriber(device="cuda").transcribe(Path("audio.wav"))


def test_sentence_transformer_embedder_returns_plain_float_lists() -> None:
    class Model:
        def encode(self, texts, normalize_embeddings):
            assert normalize_embeddings is True
            return [[1, 2.5] for _ in texts]

    assert SentenceTransformerEmbedder(model=Model()).embed(["one"]) == [[1.0, 2.5]]


def test_cross_encoder_reranker_is_injected_and_orders_candidates() -> None:
    class Model:
        def predict(self, pairs):
            assert pairs == [("question", "low"), ("question", "high")]
            return [0.1, 0.9]

    candidates = [
        {"text": "low", "metadata": {"start": 1.0, "end": 2.0}},
        {"text": "high", "metadata": {"start": 3.0, "end": 4.0}},
    ]

    results = SentenceTransformerReranker(model=Model()).rerank("question", candidates, 1)

    assert results == [{**candidates[1], "rerank_score": 0.9}]


def test_chroma_vector_store_maps_chunks_and_scopes_queries() -> None:
    class Collection:
        metadata = {"hnsw:space": "cosine"}

        def __init__(self) -> None:
            self.stored = {}
            self.deleted: list[str] = []

        def get(self):
            return {"ids": list(self.stored)}

        def delete(self, ids):
            self.deleted.extend(ids)
            for item_id in ids:
                self.stored.pop(item_id, None)

        def upsert(self, ids, documents, metadatas, embeddings):
            self.stored.update({
                item_id: {"text": text, "metadata": metadata, "embedding": embedding}
                for item_id, text, metadata, embedding in zip(ids, documents, metadatas, embeddings)
            })

        def query(self, **kwargs):
            return {
                "ids": [["0"]],
                "documents": [["fact"]],
                "metadatas": [[{"video_id": "video", "start": 1.0, "end": 2.0}]],
                "distances": [[0.1]],
                "embeddings": [[[1.0, 0.0]]],
            }

    class Client:
        def __init__(self) -> None:
            self.collection = Collection()

        def get_or_create_collection(self, name):
            assert name == "video_video"
            return self.collection

        def get_collection(self, name):
            assert name == "video_video"
            return self.collection

        def delete_collection(self, name):
            assert name == "video_video"

    client = Client()
    store = ChromaVectorStore(Path("unused"), client=client)
    chunks = [{
        "id": "0",
        "text": "fact",
        "metadata": {"video_id": "video"},
        "embedding": [1.0, 0.0],
    }]
    store.upsert("video", chunks)
    store.upsert("video", chunks)

    assert len(client.collection.stored) == 1
    assert client.collection.deleted == []
    assert store.exists("video")
    assert store.query("video", [1.0, 0.0], 5)[0]["similarity"] == 1.0
    store.upsert("video", [])
    assert client.collection.stored == {}
    assert client.collection.deleted == ["0"]
    store.delete_video("video")


def test_chroma_hybrid_query_merges_dense_and_bm25_results() -> None:
    class Collection:
        metadata = {"hnsw:space": "cosine"}

        def get(self, include=None):
            return {
                "ids": ["dense", "lexical", "neutral"],
                "documents": [
                    "generic semantic result",
                    "rare zebra keyword appears here",
                    "a third unrelated transcript passage",
                ],
                "metadatas": [
                    {"video_id": "video", "chunk_id": "dense", "start": 1.0, "end": 2.0},
                    {"video_id": "video", "chunk_id": "lexical", "start": 3.0, "end": 4.0},
                    {"video_id": "video", "chunk_id": "neutral", "start": 5.0, "end": 6.0},
                ],
            }

        def query(self, **_kwargs):
            return {
                "ids": [["dense"]],
                "documents": [["generic semantic result"]],
                "metadatas": [[{"video_id": "video", "chunk_id": "dense", "start": 1.0, "end": 2.0}]],
                "distances": [[0.1]],
                "embeddings": [[[1.0, 0.0]]],
            }

    class Client:
        def get_collection(self, name):
            assert name == "video_video"
            return Collection()

    store = ChromaVectorStore(Path("unused"), client=Client())
    results = store.query_hybrid("video", [1.0, 0.0], "zebra keyword", 5)

    assert {item["id"] for item in results} == {"dense", "lexical"}
    lexical_result = next(item for item in results if item["id"] == "lexical")
    assert lexical_result["metadata"]["start"] == 3.0
    assert lexical_result["similarity"] > 0


def test_groq_client_uses_injected_sdk_module(monkeypatch: pytest.MonkeyPatch) -> None:
    class Completions:
        def create(self, **kwargs):
            assert kwargs == {
                "messages": [{"role": "user", "content": "prompt"}],
                "model": "model",
            }
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=" reply "))])

    monkeypatch.setitem(
        sys.modules,
        "groq",
        types.SimpleNamespace(Groq=lambda api_key: SimpleNamespace(chat=SimpleNamespace(completions=Completions()))),
    )

    assert GroqLLMClient(api_key="mock-key").complete(
        [{"role": "user", "content": "prompt"}], model="model"
    ) == "reply"


def test_pyttsx3_adapter_writes_requested_path(tmp_path: Path) -> None:
    class Engine:
        def save_to_file(self, text, output_path):
            Path(output_path).write_bytes(text.encode())

        def runAndWait(self):
            pass

        def stop(self):
            pass

    engine_module = SimpleNamespace(init=lambda: Engine())
    out_path = tmp_path / "audio" / "answer.wav"

    assert Pyttsx3TTS(engine_module).synthesize("spoken", out_path) == out_path
    assert out_path.read_bytes() == b"spoken"


def test_container_composes_services_from_adapters(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    transcriber = FakeTranscriber()
    embedder = FakeEmbedder()
    vector_store = FakeVectorStore()
    llm_client = FakeLLMClient()
    tts = FakeTTS()
    blob_store = FakeBlobStore(tmp_path / "data")
    monkeypatch.setattr(
        container_module,
        "settings",
        SimpleNamespace(project_root=tmp_path, hybrid=False, rerank=False),
    )
    monkeypatch.setattr(container_module, "LocalBlobStore", lambda _root: blob_store)
    monkeypatch.setattr(container_module, "FasterWhisperTranscriber", lambda: transcriber)
    monkeypatch.setattr(container_module, "SentenceTransformerEmbedder", lambda: embedder)
    monkeypatch.setattr(container_module, "ChromaVectorStore", lambda _path: vector_store)
    monkeypatch.setattr(container_module, "GroqLLMClient", lambda: llm_client)
    monkeypatch.setattr(container_module, "Pyttsx3TTS", lambda: tts)
    container_module.get_container.cache_clear()
    container_module._get_blob_store.cache_clear()
    container_module._get_vector_store.cache_clear()

    try:
        container = container_module.get_container()
        assert container.blob_store is blob_store
        assert container.video_processing.transcriber is transcriber
        assert container.rag.embedder is embedder
        assert container.rag.vector_store is vector_store
        assert container.ai.llm.client is llm_client
        assert container.voice.tts is tts
    finally:
        container_module.get_container.cache_clear()
        container_module._get_blob_store.cache_clear()
        container_module._get_vector_store.cache_clear()
