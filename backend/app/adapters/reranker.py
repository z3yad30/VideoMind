import threading
from typing import Any

from backend.app.core.config import settings
from backend.app.ports import Reranker


class SentenceTransformerReranker:
    def __init__(self, model_name: str | None = None, model: Any | None = None) -> None:
        self.model_name = model_name or settings.rerank_model
        self._model = model
        self._lock = threading.Lock()

    def _get_model(self) -> Any:
        if self._model is None:
            with self._lock:
                if self._model is None:
                    try:
                        from sentence_transformers import CrossEncoder
                    except ImportError as exc:
                        raise RuntimeError("sentence-transformers is not installed") from exc
                    self._model = CrossEncoder(self.model_name)
        return self._model

    def rerank(
        self,
        query: str,
        candidates: list[dict[str, object]],
        top_k: int,
    ) -> list[dict[str, object]]:
        if top_k < 1 or not candidates:
            return []
        pairs = [(query, str(candidate.get("text", ""))) for candidate in candidates]
        scores = self._get_model().predict(pairs)
        if len(scores) != len(candidates):
            raise RuntimeError("Cross-encoder returned an unexpected number of scores")
        ranked = sorted(
            zip(candidates, scores),
            key=lambda item: float(item[1]),
            reverse=True,
        )
        return [
            {**candidate, "rerank_score": float(score)}
            for candidate, score in ranked[:top_k]
        ]
