import json
import re
from dataclasses import dataclass
from typing import Callable

from backend.app.ports import BlobStore, Embedder, VectorStore
from backend.app.utils.vector import normalize_distance_to_similarity

MIN_SIMILARITY = 0.70
RELATIVE_MARGIN = 0.10
DEFAULT_TOP_K = 5


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
        blob_store: BlobStore,
        embedder: Embedder,
        vector_store: VectorStore,
        max_chunk_characters: int = 1000,
    ) -> None:
        self.blob_store = blob_store
        self.embedder = embedder
        self.vector_store = vector_store
        self.max_chunk_characters = max_chunk_characters

    @staticmethod
    def collection_name(video_id: str) -> str:
        if not video_id or video_id.strip() != video_id or len(video_id) > 128:
            raise ValueError("video_id must be a non-empty string")
        return f"video_{video_id}"

    def prepare_transcript_chunks(self, video_id: str) -> list[dict[str, object]]:
        transcript_key = f"transcripts/{video_id}.json"
        if not self.blob_store.exists(transcript_key):
            return []
        try:
            transcript_path = self.blob_store.open(transcript_key)
            payload = json.loads(transcript_path.read_text(encoding="utf-8"))
            segments = payload.get("segments", [])
            if not isinstance(segments, list):
                return []
        except (OSError, json.JSONDecodeError, TypeError):
            return []

        chunks = chunk_transcript(segments, self.max_chunk_characters)
        source = str(transcript_path)
        return [
            {"id": chunk.chunk_id, "text": chunk.text, "metadata": chunk.metadata(video_id, source)}
            for chunk in chunks
        ]

    def embed_chunks(
        self,
        chunks: list[dict[str, object]],
        on_progress: Callable[[str, int, int], None] | None = None,
    ) -> list[list[float]]:
        texts = [str(chunk["text"]) for chunk in chunks]
        if on_progress:
            on_progress("embedding", 0, len(texts))
        embeddings = self.embedder.embed(texts) if texts else []
        if len(embeddings) != len(chunks):
            raise RuntimeError("Embedder returned an unexpected number of vectors")
        if on_progress:
            on_progress("embedding", len(texts), len(texts))
        return embeddings

    def index_chunks(
        self,
        video_id: str,
        chunks: list[dict[str, object]],
        embeddings: list[list[float]],
        on_progress: Callable[[str, int, int], None] | None = None,
    ) -> None:
        if len(embeddings) != len(chunks):
            raise RuntimeError("Embedding count does not match transcript chunk count")
        self.vector_store.upsert(
            video_id,
            [
                {**chunk, "embedding": embedding}
                for chunk, embedding in zip(chunks, embeddings)
            ],
        )
        if on_progress:
            on_progress("indexing", len(chunks), len(chunks))

    def index_transcript(self, video_id: str, on_progress: Callable[[str, int, int], None] | None = None) -> list[dict[str, object]]:
        chunks = self.prepare_transcript_chunks(video_id)
        if on_progress:
            on_progress("chunking", len(chunks), len(chunks))
        embeddings = self.embed_chunks(chunks, on_progress)
        self.index_chunks(video_id, chunks, embeddings, on_progress)
        return [{"text": chunk["text"], "metadata": chunk["metadata"]} for chunk in chunks]

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
        if not self.blob_store.exists(f"transcripts/{video_id}.json"):
            return []
        query_vector = self.embedder.embed([question])[0]
        candidates = self.vector_store.query(video_id, query_vector, top_k)
        return self.filter_relevant_chunks(candidates)

    def get_transcript_context(self, video_id: str) -> str:
        return self.get_transcript_context_for_question(video_id)

    def get_transcript_segments_for_question(
        self,
        video_id: str,
        question: str = "",
        max_characters: int = 12000,
    ) -> list[dict[str, object]]:
        transcript_key = f"transcripts/{video_id}.json"
        if not self.blob_store.exists(transcript_key):
            return []
        try:
            transcript_path = self.blob_store.open(transcript_key)
            payload = json.loads(transcript_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        segments = [
            segment
            for segment in (payload.get("segments", []) if isinstance(payload, dict) else [])
            if isinstance(segment, dict)
            and segment.get("text")
            and "start" in segment
            and "end" in segment
        ]
        if not segments:
            return []

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
        selected: list[dict[str, object]] = []
        character_count = 0
        for index in sorted(selected_indexes):
            segment = segments[index]
            text = str(segment["text"])
            formatted = f"[{segment['start']}-{segment['end']}] {text}"
            if selected and character_count + len(formatted) + 2 > max_characters:
                break
            selected.append({"text": text, "metadata": {"start": segment["start"], "end": segment["end"]}})
            character_count += len(formatted) + (2 if selected else 0)
        return selected

    def get_transcript_context_for_question(
        self,
        video_id: str,
        question: str = "",
        max_characters: int = 12000,
    ) -> str:
        selected = self.get_transcript_segments_for_question(video_id, question, max_characters)
        if not selected:
            return "No raw transcript is available."
        return "\n\n".join(
            f"[{item['metadata']['start']}-{item['metadata']['end']}] {item['text']}"
            for item in selected
        )

    def build_rag_context(self, video_id: str, question: str, top_k: int = 5) -> str:
        chunks = self.retrieve_relevant_chunks(video_id, question, top_k)
        return "\n\n".join(
            f"[{item['metadata']['start']}-{item['metadata']['end']}] {item['text']}" for item in chunks
        )