import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from backend.app.core.config import settings

logger = logging.getLogger(__name__)

MIN_SIMILARITY = 0.70
RELATIVE_MARGIN = 0.10
DEFAULT_TOP_K = 5


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


class LocalFallbackEmbedding:
    def __init__(self, dimensions: int = 32) -> None:
        self.dimensions = dimensions

    def _vector_for(self, text: str) -> list[float]:
        if not text:
            return [0.0] * self.dimensions

        base = text.lower().strip()
        vector = [0.0] * self.dimensions
        for index in range(self.dimensions):
            digest = hashlib.sha256(f"{base}|{index}".encode("utf-8")).digest()
            raw = int.from_bytes(digest[:8], byteorder="big", signed=False)
            vector[index] = ((raw % 2_000_000) / 1_000_000.0) - 1.0

        tokens = base.split()
        if tokens:
            vector[0] = min(len(tokens) / 20.0, 1.0)
            vector[1] = min(len(base) / 200.0, 1.0)
        return vector

    def encode(self, sentences: list[str]) -> Any:
        return [self._vector_for(sentence) for sentence in sentences]


class SentenceTransformerEmbedding:
    def __init__(self, model_name: str = "BAAI/bge-m3") -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError("sentence-transformers is not installed") from exc

        try:
            self._model = SentenceTransformer(model_name)
        except Exception as exc:  # pragma: no cover - depends on local environment
            logger.warning(
                "Could not load embedding model '%s'; using local fallback. Reason: %s",
                model_name,
                exc,
            )
            self._model = LocalFallbackEmbedding()

    def encode(self, sentences: list[str]) -> Any:
        if hasattr(self._model, "encode"):
            try:
                return self._model.encode(sentences, normalize_embeddings=True)
            except TypeError:
                return self._model.encode(sentences)
        return self._model.encode(sentences)


def embedding_rows(embeddings: Any) -> list[list[float]]:
    if hasattr(embeddings, "tolist"):
        embeddings = embeddings.tolist()
    return embeddings


def normalize_distance_to_similarity(distance: object, collection_space: str = "") -> float:
    try:
        value = float(distance)
    except (TypeError, ValueError):
        return 0.0
    if value != value:
        return 0.0
    space = (collection_space or "").lower()
    if space in {"cosine", "ip"}:
        return max(0.0, min(1.0, 1.0 - value))
    if space == "l2":
        return max(0.0, min(1.0, 1.0 / (1.0 + value)))
    if 0.0 <= value <= 1.0:
        return max(0.0, min(1.0, 1.0 - value))
    return max(0.0, min(1.0, 1.0 / (1.0 + value)))


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

    @classmethod
    def filter_relevant_chunks(
        cls,
        chunks: list[dict[str, object]],
        min_similarity: float = 0.70,
        relative_margin: float = 0.10,
    ) -> list[dict[str, object]]:
        if not chunks:
            return []
        similarities = [float(chunk.get("similarity", 0.0) or 0.0) for chunk in chunks]
        top_similarity = max(similarities, default=0.0)
        dynamic_threshold = max(min_similarity, top_similarity - relative_margin)
        return [
            chunk
            for chunk in chunks
            if float(chunk.get("similarity", 0.0) or 0.0) >= min_similarity
            and float(chunk.get("similarity", 0.0) or 0.0) >= dynamic_threshold
        ]

    def retrieve_relevant_chunks(self, video_id: str, question: str, top_k: int = 5) -> list[dict[str, object]]:
        if not question or top_k < 1:
            return []
        transcript_path = self.transcript_dir / f"{video_id}.json"
        if not transcript_path.exists():
            return []
        collection = self.client.get_collection(self.collection_name(video_id))
        query_vector = embedding_rows(self.embedding_model.encode([question]))[0]
        result = collection.query(
            query_embeddings=[query_vector],
            n_results=top_k,
            include=["documents", "metadatas", "distances", "embeddings"],
        )
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        embeddings = (result.get("embeddings") or [[]])[0]
        collection_space = ""
        metadata = getattr(collection, "metadata", None)
        if isinstance(metadata, dict):
            collection_space = str(metadata.get("hnsw:space") or metadata.get("space") or "")
        candidates: list[dict[str, object]] = []
        for index, (text, metadata) in enumerate(zip(documents, metadatas)):
            if not isinstance(metadata, dict) or metadata.get("video_id") != video_id:
                continue
            distance = distances[index] if index < len(distances) else None
            embedding = embeddings[index] if index < len(embeddings) and isinstance(embeddings[index], (list, tuple)) else None
            if embedding is not None:
                candidate_norm = sum(value * value for value in embedding) ** 0.5 or 1.0
                query_norm = sum(value * value for value in query_vector) ** 0.5 or 1.0
                cosine = sum(a * b for a, b in zip(query_vector, embedding)) / (query_norm * candidate_norm)
                similarity = max(0.0, min(1.0, cosine))
            else:
                similarity = normalize_distance_to_similarity(distance, collection_space)
            candidates.append(
                {
                    "text": text,
                    "metadata": metadata,
                    "distance": distance,
                    "similarity": similarity,
                }
            )
        return self.filter_relevant_chunks(candidates)

    def get_transcript_context(self, video_id: str) -> str:
        return self.get_transcript_context_for_question(video_id)

    def get_transcript_context_for_question(
        self,
        video_id: str,
        question: str = "",
        max_characters: int = 12000,
    ) -> str:
        transcript_path = self.transcript_dir / f"{video_id}.json"
        if not transcript_path.exists():
            return "No raw transcript is available."
        try:
            payload = json.loads(transcript_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "No raw transcript is available."
        segments = [
            segment
            for segment in (payload.get("segments", []) if isinstance(payload, dict) else [])
            if isinstance(segment, dict)
            and segment.get("text")
            and "start" in segment
            and "end" in segment
        ]
        if not segments:
            return "No raw transcript is available."

        question_terms = {
            term for term in re.findall(r"[a-z0-9]+", question.lower()) if len(term) > 2
        }
        scored = sorted(
            (
                len(question_terms.intersection(re.findall(r"[a-z0-9]+", str(segment["text"]).lower()))),
                index,
            )
            for index, segment in enumerate(segments)
        )
        selected_indexes: set[int] = set()
        for score, index in reversed(scored):
            if score == 0 and selected_indexes:
                break
            selected_indexes.update(range(max(0, index - 1), min(len(segments), index + 2)))
            context = "\n\n".join(
                f"[{segments[item]['start']}-{segments[item]['end']}] {segments[item]['text']}"
                for item in sorted(selected_indexes)
            )
            if len(context) >= max_characters:
                break

        if not selected_indexes:
            selected_indexes.update(range(min(len(segments), 12)))
        context = "\n\n".join(
            f"[{segments[index]['start']}-{segments[index]['end']}] {segments[index]['text']}"
            for index in sorted(selected_indexes)
        )
        return context[:max_characters] or "No raw transcript is available."

    def build_rag_context(self, video_id: str, question: str, top_k: int = 5) -> str:
        chunks = self.retrieve_relevant_chunks(video_id, question, top_k)
        return "\n\n".join(
            f"[{item['metadata']['start']}-{item['metadata']['end']}] {item['text']}" for item in chunks
        )