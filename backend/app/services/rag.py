import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from backend.app.core.config import settings


class EmbeddingModel(Protocol):
    def encode(self, sentences: list[str]) -> Any:
        """Encode text into vectors."""


@dataclass(frozen=True)
class TranscriptChunk:
    chunk_id: str
    text: str
    start: float
    end: float

    def metadata(self, video_id: str, source: str) -> dict[str, str | float]:
        return {
            "video_id": video_id,
            "chunk_id": self.chunk_id,
            "start": self.start,
            "end": self.end,
            "source": source,
        }


class SentenceTransformerEmbedding:
    def __init__(self, model_name: str = "BAAI/bge-m3") -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError("sentence-transformers is not installed") from exc
        self._model = SentenceTransformer(model_name)

    def encode(self, sentences: list[str]) -> Any:
        return self._model.encode(sentences, normalize_embeddings=True)


def embedding_rows(embeddings: Any) -> list[list[float]]:
    if hasattr(embeddings, "tolist"):
        embeddings = embeddings.tolist()
    return embeddings


def chunk_transcript(segments: list[dict[str, object]], max_characters: int = 1000) -> list[TranscriptChunk]:
    if max_characters < 1:
        raise ValueError("max_characters must be positive")

    chunks: list[TranscriptChunk] = []
    current_text: list[str] = []
    current_start: float | None = None
    current_end: float | None = None

    def flush() -> None:
        nonlocal current_text, current_start, current_end
        if current_text and current_start is not None and current_end is not None:
            chunks.append(TranscriptChunk(str(len(chunks)), " ".join(current_text), current_start, current_end))
        current_text = []
        current_start = None
        current_end = None

    for segment in segments:
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        start = float(segment["start"])
        end = float(segment["end"])
        would_exceed = bool(current_text) and len(" ".join((*current_text, text))) > max_characters
        if would_exceed:
            flush()
        current_text.append(text)
        current_start = start if current_start is None else current_start
        current_end = end
    flush()
    return chunks


class TranscriptRAGService:
    def __init__(
        self,
        transcript_dir: Path | None = None,
        chroma_dir: Path | None = None,
        embedding_model: EmbeddingModel | None = None,
        chroma_client: Any | None = None,
        max_chunk_characters: int = 1000,
    ) -> None:
        self.transcript_dir = transcript_dir or settings.project_root / "data" / "transcripts"
        self.chroma_dir = chroma_dir or settings.project_root / "data" / "chroma"
        self.embedding_model = embedding_model or SentenceTransformerEmbedding()
        self.max_chunk_characters = max_chunk_characters
        if chroma_client is None:
            try:
                import chromadb
            except ImportError as exc:
                raise RuntimeError("chromadb is not installed") from exc
            self.chroma_dir.mkdir(parents=True, exist_ok=True)
            chroma_client = chromadb.PersistentClient(path=str(self.chroma_dir))
        self.client = chroma_client

    @staticmethod
    def collection_name(video_id: str) -> str:
        if not video_id or video_id.strip() != video_id or len(video_id) > 128:
            raise ValueError("video_id must be a non-empty string")
        return f"video_{video_id}"

    def index_transcript(self, video_id: str, on_progress: Callable[[str, int, int], None] | None = None) -> list[dict[str, object]]:
        transcript_path = self.transcript_dir / f"{video_id}.json"
        if not transcript_path.exists():
            return []
        try:
            payload = json.loads(transcript_path.read_text(encoding="utf-8"))
            segments = payload.get("segments", [])
            if not isinstance(segments, list):
                return []
        except (OSError, json.JSONDecodeError, TypeError):
            return []

        chunks = chunk_transcript(segments, self.max_chunk_characters)
        if on_progress:
            on_progress("chunking", len(chunks), len(chunks))
        collection = self.client.get_or_create_collection(self.collection_name(video_id))
        existing = collection.get().get("ids", [])
        if existing:
            collection.delete(ids=existing)
        if not chunks:
            return []
        source = str(transcript_path)
        texts = [chunk.text for chunk in chunks]
        if on_progress:
            on_progress("embedding", 0, len(texts))
        collection.upsert(
            ids=[chunk.chunk_id for chunk in chunks],
            documents=texts,
            metadatas=[chunk.metadata(video_id, source) for chunk in chunks],
            embeddings=embedding_rows(self.embedding_model.encode(texts)),
        )
        if on_progress:
            on_progress("embedding", len(texts), len(texts))
            on_progress("indexing", len(texts), len(texts))
        return [{"text": chunk.text, "metadata": chunk.metadata(video_id, source)} for chunk in chunks]

    def retrieve_relevant_chunks(self, video_id: str, question: str, top_k: int = 5) -> list[dict[str, object]]:
        if not question or top_k < 1:
            return []
        transcript_path = self.transcript_dir / f"{video_id}.json"
        if not transcript_path.exists():
            return []
        collection = self.client.get_collection(self.collection_name(video_id))
        result = collection.query(query_embeddings=embedding_rows(self.embedding_model.encode([question])), n_results=top_k)
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        return [
            {"text": text, "metadata": metadata, "distance": distances[index] if index < len(distances) else None}
            for index, (text, metadata) in enumerate(zip(documents, metadatas))
            if isinstance(metadata, dict) and metadata.get("video_id") == video_id
        ]

    def build_rag_context(self, video_id: str, question: str, top_k: int = 5) -> str:
        chunks = self.retrieve_relevant_chunks(video_id, question, top_k)
        return "\n\n".join(
            f"[{item['metadata']['start']}-{item['metadata']['end']}] {item['text']}" for item in chunks
        )