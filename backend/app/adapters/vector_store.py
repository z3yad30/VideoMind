import re
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
        existing_ids = collection.get().get("ids", [])
        chunk_ids = {str(chunk["id"]) for chunk in chunks}
        stale_ids = [chunk_id for chunk_id in existing_ids if chunk_id not in chunk_ids]
        if stale_ids:
            collection.delete(ids=stale_ids)
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
        ids = (result.get("ids") or [[]])[0]
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
            candidate_id = ids[index] if index < len(ids) else item_metadata.get("chunk_id", str(index))
            candidates.append(
                {
                    "id": str(candidate_id),
                    "text": text,
                    "metadata": item_metadata,
                    "distance": distance,
                    "similarity": similarity,
                }
            )
        return candidates

    def query_hybrid(
        self,
        video_id: str,
        embedding: list[float],
        query_text: str,
        top_k: int,
    ) -> list[dict[str, object]]:
        try:
            from rank_bm25 import BM25Okapi
        except ImportError as exc:
            raise RuntimeError("rank-bm25 is required when HYBRID=true") from exc

        dense_candidates = self.query(video_id, embedding, top_k)
        def tokenize(text: str) -> list[str]:
            return re.findall(r"\w+", text.casefold())

        collection = self.client.get_collection(self.collection_name(video_id))
        stored = collection.get(include=["documents", "metadatas"])
        ids = stored.get("ids") or []
        documents = stored.get("documents") or []
        metadatas = stored.get("metadatas") or []
        rows = [
            (str(chunk_id), text, metadata)
            for chunk_id, text, metadata in zip(ids, documents, metadatas)
            if isinstance(text, str)
            and isinstance(metadata, dict)
            and metadata.get("video_id") == video_id
        ]
        if not rows or not any(tokenize(text) for _, text, _ in rows):
            return dense_candidates

        tokenized_documents = [tokenize(text) for _, text, _ in rows]
        query_tokens = tokenize(query_text)
        lexical_scores = BM25Okapi(tokenized_documents).get_scores(query_tokens) if query_tokens else []
        lexical_ranked = sorted(
            (
                (rows[index][0], float(score))
                for index, score in enumerate(lexical_scores)
                if score > 0
            ),
            key=lambda item: (-item[1], item[0]),
        )[:top_k]

        merged: dict[str, dict[str, object]] = {}
        for rank, candidate in enumerate(dense_candidates, start=1):
            candidate_id = str(candidate.get("id", ""))
            if not candidate_id:
                metadata = candidate.get("metadata")
                candidate_id = str(metadata.get("chunk_id", rank)) if isinstance(metadata, dict) else str(rank)
            merged[candidate_id] = {**candidate, "_dense_rank": rank}

        for rank, (chunk_id, _score) in enumerate(lexical_ranked, start=1):
            row = next(item for item in rows if item[0] == chunk_id)
            candidate = merged.setdefault(
                chunk_id,
                {
                    "id": chunk_id,
                    "text": row[1],
                    "metadata": row[2],
                    "distance": None,
                    "similarity": 0.0,
                },
            )
            candidate["_lexical_rank"] = rank

        if not merged:
            return []
        for candidate in merged.values():
            rrf_score = sum(
                1.0 / (60 + int(candidate[f"_{rank_key}"]))
                for rank_key in ("dense_rank", "lexical_rank")
                if f"_{rank_key}" in candidate
            )
            candidate["rrf_score"] = rrf_score
        ordered = sorted(
            merged.values(),
            key=lambda item: (-float(item["rrf_score"]), str(item["id"])),
        )[:top_k]
        highest_score = float(ordered[0]["rrf_score"])
        for candidate in ordered:
            normalized = float(candidate["rrf_score"]) / highest_score if highest_score else 0.0
            candidate["dense_similarity"] = candidate["similarity"]
            candidate["similarity"] = normalized
        return ordered

    def exists(self, video_id: str) -> bool:
        try:
            self.client.get_collection(self.collection_name(video_id))
            return True
        except Exception as exc:
            if exc.__class__.__name__ in {"InvalidCollectionException", "NotFoundError"}:
                return False
            raise

    def delete_video(self, video_id: str) -> None:
        name = self.collection_name(video_id)
        try:
            self.client.delete_collection(name)
        except Exception as exc:
            if exc.__class__.__name__ not in {"InvalidCollectionException", "NotFoundError"}:
                raise
