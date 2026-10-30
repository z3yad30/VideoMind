from pathlib import Path
from typing import Any

from backend.app.ports import VectorStore
from backend.app.utils.vector import normalize_distance_to_similarity


class ChromaVectorStore:
    def __init__(self, chroma_dir: Path, client: Any | None = None) -> None:
        if client is None:
            try:
                import chromadb
            except ImportError as exc:
                raise RuntimeError("chromadb is not installed") from exc
            chroma_dir.mkdir(parents=True, exist_ok=True)
            client = chromadb.PersistentClient(path=str(chroma_dir))
        self.client = client

    @staticmethod
    def collection_name(video_id: str) -> str:
        if not video_id or video_id.strip() != video_id or len(video_id) > 128:
            raise ValueError("video_id must be a non-empty string")
        return f"video_{video_id}"

    def upsert(self, video_id: str, chunks: list[dict[str, object]]) -> None:
        collection = self.client.get_or_create_collection(self.collection_name(video_id))
        existing = collection.get().get("ids", [])
        if existing:
            collection.delete(ids=existing)
        if chunks:
            collection.upsert(
                ids=[str(chunk["id"]) for chunk in chunks],
                documents=[str(chunk["text"]) for chunk in chunks],
                metadatas=[chunk["metadata"] for chunk in chunks],
                embeddings=[chunk["embedding"] for chunk in chunks],
            )

    def query(self, video_id: str, embedding: list[float], top_k: int) -> list[dict[str, object]]:
        collection = self.client.get_collection(self.collection_name(video_id))
        result = collection.query(
            query_embeddings=[embedding],
            n_results=top_k,
            include=["documents", "metadatas", "distances", "embeddings"],
        )
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        embeddings = (result.get("embeddings") or [[]])[0]
        metadata = getattr(collection, "metadata", None)
        space = str(metadata.get("hnsw:space") or metadata.get("space") or "") if isinstance(metadata, dict) else ""
        candidates: list[dict[str, object]] = []
        for index, (text, item_metadata) in enumerate(zip(documents, metadatas)):
            if not isinstance(item_metadata, dict) or item_metadata.get("video_id") != video_id:
                continue
            distance = distances[index] if index < len(distances) else None
            item_embedding = embeddings[index] if index < len(embeddings) else None
            if isinstance(item_embedding, (list, tuple)):
                candidate_norm = sum(value * value for value in item_embedding) ** 0.5 or 1.0
                query_norm = sum(value * value for value in embedding) ** 0.5 or 1.0
                similarity = max(
                    0.0,
                    min(1.0, sum(a * b for a, b in zip(embedding, item_embedding)) / (query_norm * candidate_norm)),
                )
            else:
                similarity = normalize_distance_to_similarity(distance, space)
            candidates.append({"text": text, "metadata": item_metadata, "distance": distance, "similarity": similarity})
        return candidates

    def delete_video(self, video_id: str) -> None:
        name = self.collection_name(video_id)
        try:
            self.client.delete_collection(name)
        except Exception as exc:
            if exc.__class__.__name__ not in {"InvalidCollectionException", "NotFoundError"}:
                raise
