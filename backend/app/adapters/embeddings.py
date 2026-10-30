import hashlib
import logging
from typing import Any

from backend.app.ports import Embedder

logger = logging.getLogger(__name__)


class _LocalFallbackEmbedding:
    def __init__(self, dimensions: int = 32) -> None:
        self.dimensions = dimensions

    def encode(self, text: str) -> list[float]:
        if not text:
            return [0.0] * self.dimensions
        base = text.lower().strip()
        vector = []
        for index in range(self.dimensions):
            digest = hashlib.sha256(f"{base}|{index}".encode("utf-8")).digest()
            raw = int.from_bytes(digest[:8], byteorder="big", signed=False)
            vector.append(((raw % 2_000_000) / 1_000_000.0) - 1.0)
        tokens = base.split()
        if tokens:
            vector[0] = min(len(tokens) / 20.0, 1.0)
            vector[1] = min(len(base) / 200.0, 1.0)
        return vector


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str = "BAAI/bge-m3", model: Any | None = None) -> None:
        if model is not None:
            self._model = model
            return
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
            self._model = _LocalFallbackEmbedding()

    def embed(self, texts: list[str]) -> list[list[float]]:
        if isinstance(self._model, _LocalFallbackEmbedding):
            return [self._model.encode(text) for text in texts]
        try:
            embeddings = self._model.encode(texts, normalize_embeddings=True)
        except TypeError:
            embeddings = self._model.encode(texts)
        if hasattr(embeddings, "tolist"):
            embeddings = embeddings.tolist()
        return [[float(value) for value in row] for row in embeddings]
